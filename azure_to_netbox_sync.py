#!/usr/bin/env python3
"""
azure_to_netbox_sync.py

Single-file sync job: reads all VNet address-space CIDRs from Azure and
reconciles them into NetBox as IPAM Prefixes, so NetBox stays an accurate,
automatically-updated source of truth (replaces manual Confluence tracking).

SCOPE NOTE: this syncs VNet-level address spaces only (Azure's
vnet.address_space.address_prefixes) - it does NOT currently sync
individual subnet CIDRs within each VNet. If subnet-level tracking is
needed too, that's a well-defined but separate extension (walk
vnet.subnets and create each as a child Prefix under the VNet's Prefix
in NetBox) - ask if you want that added.

This script also manages its OWN daily cron schedule on the same machine
NetBox runs on - no separate Container Apps Job or external scheduler
needed. See "Cron setup" below.

This script also self-installs its own Python dependencies on first run -
no separate `pip install -r requirements.txt` step needed. Set
SKIP_AUTO_INSTALL=1 to disable this if you're managing dependencies
yourself (e.g. a pre-built container image).

Design principles:
  - READ-ONLY on the Azure side (Reader role is sufficient).
  - NEVER auto-deletes anything in NetBox. Prefixes that exist in NetBox
    but were not seen in this run are tagged 'stale' for human review,
    not removed. Automated deletion of infra records is too risky.
  - Idempotent: safe to run daily, or re-run manually, without creating
    duplicates or clobbering manual edits to fields it doesn't own.
  - Every prefix it manages is tagged 'azure-sync' so it's obvious in the
    NetBox UI which records are automated vs. manually created.

--------------------------------------------------------------------------
Configuration
--------------------------------------------------------------------------
Required (either as real environment variables, OR as KEY=VALUE lines in
a config file - see --config below, which is what cron will use since
cron jobs don't inherit your shell's environment):

  AZURE_SUBSCRIPTION_ID   Azure subscription to scan (comma-separate for multiple)
  NETBOX_URL              e.g. https://netbox.internal.yourcompany.com
  NETBOX_TOKEN            NetBox API token (IPAM read/write scope only)

Optional:
  AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET
      Only needed if not using a managed identity (DefaultAzureCredential
      picks these up automatically if set; otherwise it falls back to
      managed identity / az cli login / etc.)

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------
  # TRUE ONE-COMMAND SETUP on a fresh Ubuntu/Debian VM (run as root):
  #   - installs Docker + Compose plugin
  #   - deploys NetBox (writes docker-compose.yml, pulls images, starts it)
  #   - creates the NetBox admin user + API token automatically
  #   - writes the sync config file
  #   - installs the daily cron job
  sudo python3 azure_to_netbox_sync.py --bootstrap

  # Same, but also auto-create the Azure service principal (Reader role).
  # Requires you to have already run `az login` once by hand first - that
  # one interactive auth step is the only thing that can't be automated.
  sudo python3 azure_to_netbox_sync.py --bootstrap --create-azure-sp --azure-subscription-id <sub-id>

  # --- Or, if you already have NetBox running and just want the sync piece: ---

  # One-time: create a config file with your real values
  python3 azure_to_netbox_sync.py --init-config
  vi netbox-azure-sync.env          # fill in the placeholder values

  # Run once by hand first, to verify output before trusting the schedule
  python3 azure_to_netbox_sync.py --config netbox-azure-sync.env

  # Install the daily cron job (this machine, no external scheduler needed)
  python3 azure_to_netbox_sync.py --install-cron --config /etc/netbox-azure-sync/netbox-azure-sync.env

  # Remove the cron job later if needed
  python3 azure_to_netbox_sync.py --uninstall-cron

Exit codes:
  0  success
  1  auth, connectivity, or setup failure
  2  partial failure (some prefixes failed to sync) -- check logs
"""

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Self-installing dependencies - makes this a true single-file tool. No
# separate `pip install -r requirements.txt` step needed first; missing
# packages are installed automatically on first run.
# Set SKIP_AUTO_INSTALL=1 to disable (e.g. a CI image that already manages
# dependencies explicitly and shouldn't install anything at runtime).
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = [
    "azure-identity>=1.17",
    "azure-mgmt-network>=27.0",
    "pynetbox>=7.3",
]


def _ensure_pip_available() -> None:
    """
    Some minimal/cloud-image Ubuntu & Debian installs ship WITHOUT pip at all
    (python3-pip isn't in the base image) - in that case 'python3 -m pip ...'
    fails with 'No module named pip' regardless of --break-system-packages,
    since the problem isn't PEP 668, it's that pip doesn't exist yet. Detect
    that specific case and install python3-pip via apt before proceeding.
    """
    check = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True)
    if check.returncode == 0:
        return  # pip already available

    print("[bootstrap] pip is not installed - attempting to install python3-pip via apt...", file=sys.stderr)

    if not os.path.exists("/etc/debian_version"):
        print(
            "[bootstrap] ERROR: pip is missing and this isn't a Debian/Ubuntu system "
            "(no apt available to auto-install python3-pip). Install pip manually for "
            "this Python interpreter, then re-run.", file=sys.stderr,
        )
        sys.exit(1)

    if os.geteuid() != 0:
        print(
            "[bootstrap] ERROR: pip is missing and installing python3-pip requires root. "
            "Run: sudo apt-get update && sudo apt-get install -y python3-pip", file=sys.stderr,
        )
        sys.exit(1)

    try:
        subprocess.run(["apt-get", "update", "-qq"], check=True)
        subprocess.run(["apt-get", "install", "-y", "-qq", "python3-pip"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"[bootstrap] ERROR: failed to install python3-pip automatically: {e}", file=sys.stderr)
        print("[bootstrap] Try manually: sudo apt-get update && sudo apt-get install -y python3-pip", file=sys.stderr)
        sys.exit(1)

    # Confirm it actually worked before proceeding
    check = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True)
    if check.returncode != 0:
        print("[bootstrap] ERROR: python3-pip installed but 'python3 -m pip' still doesn't work. Investigate manually.", file=sys.stderr)
        sys.exit(1)

    print("[bootstrap] pip installed successfully.", file=sys.stderr)


def _ensure_dependencies() -> None:
    if os.environ.get("SKIP_AUTO_INSTALL"):
        return
    try:
        import azure.identity          # noqa: F401
        import azure.mgmt.network      # noqa: F401
        import pynetbox                # noqa: F401
        return  # already installed - nothing to do
    except ImportError:
        pass

    _ensure_pip_available()

    print("[bootstrap] Required Python packages not found - installing now...", file=sys.stderr)
    base_cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + REQUIRED_PACKAGES
    try:
        # --break-system-packages is required on modern Debian/Ubuntu (PEP 668-managed
        # environments). Try it first since that's the common case for a VM deployment.
        subprocess.run(base_cmd + ["--break-system-packages"], check=True)
    except subprocess.CalledProcessError:
        # Fallback for environments where that flag isn't recognized (e.g. inside
        # a virtualenv, where it's unnecessary and some pip versions reject it).
        # NOTE: some orgs' security policy blocks --break-system-packages
        # entirely (it installs into the system Python, bypassing PEP 668's
        # protection). If that's the case here, create and activate a venv
        # BEFORE running this script instead (`python3 -m venv .venv && source
        # .venv/bin/activate`) - _ensure_dependencies() will then install into
        # the venv cleanly without needing --break-system-packages at all.
        subprocess.run(base_cmd, check=True)
    print("[bootstrap] Dependencies installed successfully.", file=sys.stderr)


_ensure_dependencies()

from azure.identity import DefaultAzureCredential
from azure.mgmt.network import NetworkManagementClient
import pynetbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("netbox-azure-sync")

SYNC_TAG = "azure-sync"
STALE_TAG = "stale-review"
CRON_MARKER = "# managed-by:azure_to_netbox_sync.py"
DEFAULT_CRON_SCHEDULE = "0 3 * * *"  # 03:00 daily - off-peak default
# NOTE: cron runs on the SERVER's local timezone, not necessarily UTC. Check
# `timedatectl` on the VM before assuming this schedule means 03:00 UTC -
# if the VM is set to a local timezone, adjust the schedule (or the VM's
# timezone) accordingly.


@dataclass
class DiscoveredPrefix:
    prefix: str          # CIDR, e.g. "10.20.0.0/16"
    vnet_name: str
    resource_group: str
    subscription_id: str


def parse_resource_group(azure_resource_id: str) -> str:
    """
    Safely extract the resource group name from an Azure resource ID, e.g.:
    /subscriptions/{sub}/resourceGroups/{rg}/providers/... -> {rg}
    Searches for the 'resourceGroups' segment explicitly rather than assuming
    a fixed index position, and falls back to 'unknown' + a warning instead
    of raising if the ID doesn't match the expected shape.
    """
    parts = azure_resource_id.split("/")
    try:
        idx = [p.lower() for p in parts].index("resourcegroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        log.warning(f"Could not parse resource group from ID: {azure_resource_id}")
        return "unknown"


# ---------------------------------------------------------------------------
# Config file support (cron jobs don't inherit your shell's env vars, so
# this lets the script load its own config from a file instead)
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# netbox-azure-sync configuration
# Fill in real values, then keep this file readable only by the user that
# runs the cron job (chmod 600) since it contains a credential.
#
# SECURITY NOTE: these secrets are stored in PLAINTEXT on disk (mode 600
# mitigates casual access, but is not equivalent to a real secrets manager).
# For a single VM this is a reasonable tradeoff for simplicity. If your org
# has stricter requirements, consider pulling NETBOX_TOKEN / AZURE_CLIENT_SECRET
# from Azure Key Vault (or another vault) at cron runtime instead of storing
# them here directly - this file's KEY=VALUE format still works as the
# integration point, just populate it from the vault right before each run
# instead of once by hand.

AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000
NETBOX_URL=https://netbox.internal.yourcompany.com
NETBOX_TOKEN=REPLACE_WITH_NETBOX_API_TOKEN

# Optional - only needed if not using a managed identity:
# AZURE_TENANT_ID=
# AZURE_CLIENT_ID=
# AZURE_CLIENT_SECRET=
"""


def load_config_file(path: str) -> None:
    """Parse simple KEY=VALUE lines from a config file into os.environ.
    Real environment variables that are already set take priority and are
    NOT overwritten - this makes the file the cron-safe default while still
    letting you override via the shell for a one-off manual run/test."""
    if not os.path.exists(path):
        log.error(f"Config file not found: {path}")
        sys.exit(1)

    with open(path) as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                log.warning(f"Skipping malformed config line {line_num} in {path}")
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

    log.info(f"Loaded config from {path}")


def init_config(path: str) -> None:
    """Write a starter config file with placeholder values."""
    if os.path.exists(path):
        log.error(f"{path} already exists - refusing to overwrite. Remove it first if you want a fresh template.")
        sys.exit(1)
    with open(path, "w") as f:
        f.write(CONFIG_TEMPLATE)
    os.chmod(path, 0o600)  # contains a credential - restrict to owner only
    log.info(f"Wrote config template to {path} (mode 600). Edit it with real values before running.")


# ---------------------------------------------------------------------------
# Cron self-management - install/uninstall a crontab entry for this script
# ---------------------------------------------------------------------------

def _read_current_crontab() -> str:
    """Return the current user's crontab text, or empty string if none exists yet."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        # `crontab -l` exits non-zero when no crontab exists yet for this user - that's fine
        return ""
    return result.stdout


def _write_crontab(text: str) -> None:
    result = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if result.returncode != 0:
        log.error(f"Failed to update crontab: {result.stderr}")
        sys.exit(1)


def build_cron_line(schedule: str, config_path: str, log_path: str) -> str:
    """Build the crontab line that runs this exact script on a schedule."""
    script_path = os.path.abspath(__file__)
    python_bin = sys.executable
    config_path = os.path.abspath(config_path)
    log_path = os.path.abspath(log_path)
    return (
        f"{schedule} {python_bin} {script_path} --config {config_path} "
        f">> {log_path} 2>&1 {CRON_MARKER}"
    )


def install_cron(schedule: str, config_path: str, log_path: str) -> None:
    """Add (or replace) this script's entry in the current user's crontab."""
    if not os.path.exists(config_path):
        log.error(
            f"Config file {config_path} does not exist yet. "
            f"Run with --init-config first, fill in real values, then retry --install-cron."
        )
        sys.exit(1)

    new_line = build_cron_line(schedule, config_path, log_path)
    current = _read_current_crontab()

    # Remove any previous entry this script installed (idempotent re-install)
    kept_lines = [line for line in current.splitlines() if CRON_MARKER not in line]
    kept_lines.append(new_line)
    updated = "\n".join(kept_lines) + "\n"

    _write_crontab(updated)
    log.info(f"Cron job installed: {new_line}")
    log.info("NOTE: this schedule runs in the server's LOCAL timezone (check `timedatectl`), not necessarily UTC.")
    log.info(f"Logs will be written to: {os.path.abspath(log_path)}")


def uninstall_cron() -> None:
    """Remove this script's entry from the current user's crontab, if present."""
    current = _read_current_crontab()
    kept_lines = [line for line in current.splitlines() if CRON_MARKER not in line]

    if len(kept_lines) == len(current.splitlines()):
        log.info("No cron entry for this script was found - nothing to remove.")
        return

    updated = "\n".join(kept_lines)
    if updated:
        updated += "\n"
    _write_crontab(updated)
    log.info("Cron job removed.")


# ---------------------------------------------------------------------------
# Full VM bootstrap - installs Docker, deploys NetBox, pulls images, creates
# the admin user + API token automatically, and (optionally) an Azure
# service principal. Goal: after copying this ONE file to a fresh Ubuntu/
# Debian VM, `sudo python3 azure_to_netbox_sync.py --bootstrap` is the only
# command that needs to be run by hand.
#
# The one thing that genuinely cannot be automated away: authenticating to
# Azure the first time (`az login`) requires a human to approve it in a
# browser or enter a device code - there is no way around this without an
# existing credential, which is exactly what we're trying to create. Every
# other step below runs unattended.
# ---------------------------------------------------------------------------

NETBOX_COMPOSE_YAML = """\
services:
  netbox:
    image: netboxcommunity/netbox:v4.1
    depends_on:
      - postgres
      - redis
      - redis-cache
    env_file: netbox.env
    ports:
      - "8000:8080"
    volumes:
      - netbox-media:/opt/netbox/netbox/media
      - netbox-reports:/opt/netbox/netbox/reports
      - netbox-scripts:/opt/netbox/netbox/scripts
    restart: unless-stopped
  postgres:
    image: postgres:16-alpine
    env_file: netbox.env
    volumes:
      - netbox-postgres-data:/var/lib/postgresql/data
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    command: --appendonly yes
    volumes:
      - netbox-redis-data:/data
    restart: unless-stopped
  redis-cache:
    image: redis:7-alpine
    restart: unless-stopped

volumes:
  netbox-media:
  netbox-reports:
  netbox-scripts:
  netbox-postgres-data:
  netbox-redis-data:
"""

NETBOX_ENV_TEMPLATE = """\
# Auto-generated by azure_to_netbox_sync.py --bootstrap
SECRET_KEY={secret_key}
ALLOWED_HOSTS=*
DB_NAME=netbox
DB_USER=netbox
DB_PASSWORD={db_password}
DB_HOST=postgres
POSTGRES_DB=netbox
POSTGRES_USER=netbox
POSTGRES_PASSWORD={db_password}
REDIS_HOST=redis
REDIS_CACHE_HOST=redis-cache
SKIP_SUPERUSER=true
"""


def require_root() -> None:
    if os.geteuid() != 0:
        log.error("--bootstrap installs system packages and must be run as root, e.g.: sudo python3 azure_to_netbox_sync.py --bootstrap")
        sys.exit(1)


def _command_succeeds(cmd: list[str]) -> bool:
    """
    Safely check whether a command runs successfully - treats BOTH a
    non-zero exit code AND the binary not existing at all as "not available".

    subprocess.run raises FileNotFoundError (not just a non-zero return
    code) when the executable itself isn't found on PATH - this matters a
    lot here, since these checks run on fresh VMs specifically to detect
    "is docker/az installed yet", where the answer is very often "no, not
    even the binary exists". Checking only .returncode without catching
    this exception crashes the whole bootstrap instead of correctly
    proceeding to the install step.
    """
    try:
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that logs the command and raises on failure."""
    log.info(f"[bootstrap] $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def install_docker() -> None:
    """Install Docker Engine + Compose plugin on a Debian/Ubuntu VM, if not already present."""
    if _command_succeeds(["docker", "compose", "version"]):
        log.info("[bootstrap] Docker + Compose plugin already installed - skipping.")
        return

    if not os.path.exists("/etc/debian_version"):
        log.error(
            "[bootstrap] This bootstrap only supports Debian/Ubuntu (apt-based) VMs. "
            "Install Docker manually on other distros, then re-run --bootstrap - it will "
            "detect Docker is present and skip this step."
        )
        sys.exit(1)

    log.info("[bootstrap] Installing Docker Engine + Compose plugin...")
    run_cmd(["apt-get", "update", "-qq"])
    run_cmd(["apt-get", "install", "-y", "-qq", "ca-certificates", "curl", "gnupg"])
    run_cmd(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
    # Official Docker install script - handles the correct repo/key setup for the
    # detected distro version, avoiding hand-rolled apt-key/repo logic going stale.
    run_cmd(["bash", "-c", "curl -fsSL https://get.docker.com | sh"])
    run_cmd(["systemctl", "enable", "docker"])
    run_cmd(["systemctl", "start", "docker"])
    log.info("[bootstrap] Docker installed and enabled on boot.")


def write_netbox_files(netbox_dir: str) -> dict:
    """Write the embedded docker-compose.yml + env file for NetBox. Returns generated secrets."""
    os.makedirs(netbox_dir, exist_ok=True)
    import secrets as _secrets

    secret_key = _secrets.token_urlsafe(50)
    db_password = _secrets.token_urlsafe(24)
    admin_password = _secrets.token_urlsafe(16)

    compose_path = os.path.join(netbox_dir, "docker-compose.yml")
    env_path = os.path.join(netbox_dir, "netbox.env")

    if not os.path.exists(compose_path):
        with open(compose_path, "w") as f:
            f.write(NETBOX_COMPOSE_YAML)
        log.info(f"[bootstrap] Wrote {compose_path}")
    else:
        log.info(f"[bootstrap] {compose_path} already exists - leaving it as-is.")

    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(NETBOX_ENV_TEMPLATE.format(secret_key=secret_key, db_password=db_password))
        os.chmod(env_path, 0o600)
        log.info(f"[bootstrap] Wrote {env_path} (mode 600)")
    else:
        log.info(f"[bootstrap] {env_path} already exists - leaving it as-is (secrets not regenerated).")

    return {"admin_password": admin_password}


def docker_compose_up(netbox_dir: str) -> None:
    """Pull NetBox's images and start the stack."""
    log.info("[bootstrap] Pulling Docker images (netbox, postgres, redis)...")
    run_cmd(["docker", "compose", "pull"], cwd=netbox_dir)
    log.info("[bootstrap] Starting NetBox stack...")
    run_cmd(["docker", "compose", "up", "-d"], cwd=netbox_dir)


def wait_for_netbox_ready(url: str, timeout_seconds: int = 180) -> None:
    """Poll NetBox's login page until it responds, or give up after timeout_seconds."""
    import time
    import urllib.request
    import urllib.error

    log.info(f"[bootstrap] Waiting for NetBox to become ready at {url} ...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status < 500:
                    log.info("[bootstrap] NetBox is responding.")
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(5)
    log.error(f"[bootstrap] NetBox did not become ready within {timeout_seconds}s. Check: docker compose logs netbox")
    sys.exit(1)


def create_netbox_superuser_and_token(netbox_dir: str, admin_password: str) -> str:
    """
    Non-interactively create the NetBox admin user (if not already present) and
    an API token for it, using Django's standard createsuperuser --noinput env
    vars. Returns the API token string.

    Retries a few times with a short delay: even after NetBox's HTTP endpoint
    starts responding (wait_for_netbox_ready), database migrations can still
    be finishing on a truly fresh container on first boot, which can cause a
    transient "relation does not exist" / connection error on the very first
    manage.py call. This is a real first-run race condition, not hypothetical -
    retrying a few times is cheap insurance against a bootstrap that otherwise
    looked successful up to this point.
    """
    import time

    # IMPORTANT: `docker compose exec` does NOT forward the host process's
    # environment into the container automatically - setting env=... on
    # subprocess.run() only affects the `docker compose exec` process itself,
    # not what manage.py sees inside the container. The variables must be
    # passed explicitly via `-e KEY=VALUE` flags on the exec command itself.
    superuser_env_flags = [
        "-e", "DJANGO_SUPERUSER_USERNAME=admin",
        "-e", "DJANGO_SUPERUSER_EMAIL=admin@example.local",
        "-e", f"DJANGO_SUPERUSER_PASSWORD={admin_password}",
    ]

    log.info("[bootstrap] Creating NetBox admin user (idempotent - skips if it already exists)...")
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            ["docker", "compose", "exec", "-T"] + superuser_env_flags + ["netbox",
             "/opt/netbox/netbox/manage.py", "createsuperuser", "--noinput"],
            cwd=netbox_dir, capture_output=True, text=True,
        )
        stderr = result.stderr or ""
        if result.returncode == 0 or "already exists" in stderr:
            break
        if attempt == max_attempts:
            log.error(f"[bootstrap] Failed to create admin user after {max_attempts} attempts: {stderr}")
            sys.exit(1)
        log.warning(
            f"[bootstrap] createsuperuser attempt {attempt}/{max_attempts} failed "
            f"(likely still finishing DB migrations) - retrying in 10s: {stderr.strip()[:200]}"
        )
        time.sleep(10)

    log.info("[bootstrap] Creating/retrieving API token for admin user...")
    token_script = (
        "from django.contrib.auth import get_user_model\n"
        "from users.models import Token\n"
        "user = get_user_model().objects.get(username='admin')\n"
        "token, _ = Token.objects.get_or_create(user=user)\n"
        "print('TOKEN_VALUE:' + token.key)\n"
    )

    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "netbox",
             "/opt/netbox/netbox/manage.py", "shell", "-c", token_script],
            cwd=netbox_dir, capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("TOKEN_VALUE:"):
                return line.split("TOKEN_VALUE:", 1)[1].strip()
        if attempt == max_attempts:
            break
        log.warning(f"[bootstrap] Token creation attempt {attempt}/{max_attempts} did not return a token - retrying in 10s.")
        time.sleep(10)

    log.error("[bootstrap] Could not extract API token from NetBox shell output.")
    sys.exit(1)


def create_azure_service_principal(subscription_id: str, sp_name: str = "netbox-azure-sync") -> dict:
    """
    Create a Reader-only Azure service principal for the sync job, via the
    Azure CLI. Requires `az` to be installed AND the operator to already be
    logged in (`az login`) - this is the one step in the whole bootstrap
    that genuinely needs a human to authenticate interactively at least
    once; there's no way to create a credential without one.
    """
    if not _command_succeeds(["az", "account", "show"]):
        log.error(
            "[bootstrap] Azure CLI isn't installed, or you're not logged in. If 'az' was just "
            "installed, run 'az login' interactively once (this is the one manual step that "
            "can't be automated), then re-run with --bootstrap --create-azure-sp."
        )
        sys.exit(1)

    log.info(f"[bootstrap] Creating Azure service principal '{sp_name}' with Reader role...")
    import json as _json
    result = run_cmd(
        ["az", "ad", "sp", "create-for-rbac", "--name", sp_name, "--role", "Reader",
         "--scopes", f"/subscriptions/{subscription_id}", "--output", "json"],
        capture_output=True, text=True,
    )
    creds = _json.loads(result.stdout)
    return {
        "AZURE_CLIENT_ID": creds["appId"],
        "AZURE_TENANT_ID": creds["tenant"],
        "AZURE_CLIENT_SECRET": creds["password"],
    }


def bootstrap(args) -> None:
    require_root()
    netbox_dir = args.netbox_dir

    install_docker()
    secrets_out = write_netbox_files(netbox_dir)
    docker_compose_up(netbox_dir)
    wait_for_netbox_ready(f"http://localhost:{args.netbox_port}/")
    token = create_netbox_superuser_and_token(netbox_dir, secrets_out["admin_password"])

    config_lines = [
        "# Auto-generated by azure_to_netbox_sync.py --bootstrap",
        f"NETBOX_URL=http://localhost:{args.netbox_port}",
        f"NETBOX_TOKEN={token}",
    ]

    if args.create_azure_sp:
        if not args.azure_subscription_id:
            log.error("--create-azure-sp requires --azure-subscription-id")
            sys.exit(1)
        if not _command_succeeds(["az", "--version"]):
            log.info("[bootstrap] Installing Azure CLI...")
            # Official Microsoft install script for Debian/Ubuntu (apt-based), per
            # https://learn.microsoft.com/cli/azure/install-azure-cli-linux
            run_cmd(["bash", "-c", "curl -fsSL 'https://azurecliprod.blob.core.windows.net/$root/deb_install.sh' | bash"])
        sp_creds = create_azure_service_principal(args.azure_subscription_id)
        config_lines.append(f"AZURE_SUBSCRIPTION_ID={args.azure_subscription_id}")
        for k, v in sp_creds.items():
            config_lines.append(f"{k}={v}")
    else:
        config_lines.append("# Fill these in manually (or re-run with --create-azure-sp):")
        config_lines.append("AZURE_SUBSCRIPTION_ID=REPLACE_ME")

    config_path = os.path.abspath(args.config or "netbox-azure-sync.env")
    with open(config_path, "w") as f:
        f.write("\n".join(config_lines) + "\n")
    os.chmod(config_path, 0o600)
    log.info(f"[bootstrap] Wrote sync config to {config_path} (mode 600)")

    install_cron(args.cron_schedule, config_path, args.log_file)

    log.info("=" * 70)
    log.info("[bootstrap] DONE.")
    log.info(f"  NetBox UI:      http://<this-vm>:{args.netbox_port}  (user: admin / password: {secrets_out['admin_password']})")
    log.info(f"  Sync config:    {config_path}")
    log.info(f"  Cron schedule:  {args.cron_schedule}")
    if not args.create_azure_sp:
        log.info("  REMAINING MANUAL STEP: edit the config file and fill in AZURE_SUBSCRIPTION_ID "
                  "plus either a managed identity or AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET.")
    log.info("=" * 70)


def get_env(name: str, required: bool = True, default: str = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error(f"Missing required environment variable: {name}")
        sys.exit(1)
    return val


def discover_azure_prefixes(subscription_ids: list[str]) -> list[DiscoveredPrefix]:
    """Enumerate all VNet address spaces across the given subscriptions."""
    credential = DefaultAzureCredential()
    discovered: list[DiscoveredPrefix] = []

    for sub_id in subscription_ids:
        log.info(f"Scanning subscription {sub_id} ...")
        try:
            client = NetworkManagementClient(credential, sub_id)
            for vnet in client.virtual_networks.list_all():
                # Safely extract the resource group (searches for the
                # 'resourceGroups' segment rather than assuming a fixed index)
                resource_group = parse_resource_group(vnet.id)
                for cidr in vnet.address_space.address_prefixes:
                    discovered.append(
                        DiscoveredPrefix(
                            prefix=cidr,
                            vnet_name=vnet.name,
                            resource_group=resource_group,
                            subscription_id=sub_id,
                        )
                    )
        except Exception as e:
            log.error(f"Failed to enumerate subscription {sub_id}: {e}")
            raise

    log.info(f"Discovered {len(discovered)} address prefixes across Azure")
    return discovered


def ensure_tags_exist(nb: pynetbox.api):
    """Make sure our management tags exist in NetBox before we use them."""
    for tag_slug, tag_name, color in [
        (SYNC_TAG, "Azure Sync", "2196f3"),
        (STALE_TAG, "Stale - Needs Review", "f44336"),
    ]:
        if not nb.extras.tags.get(slug=tag_slug):
            nb.extras.tags.create(name=tag_name, slug=tag_slug, color=color)
            log.info(f"Created NetBox tag '{tag_slug}'")


def sync_to_netbox(nb: pynetbox.api, discovered: list[DiscoveredPrefix]) -> dict:
    """Create/update NetBox prefixes to match discovered Azure state.
    Returns a summary dict of counts."""
    summary = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0, "stale_flagged": 0}
    seen_prefixes = set()

    for item in discovered:
        seen_prefixes.add(item.prefix)
        description = f"{item.vnet_name} ({item.resource_group})"

        try:
            existing = nb.ipam.prefixes.get(prefix=item.prefix)
            if existing:
                changed = False
                if existing.description != description:
                    existing.description = description
                    changed = True

                # NetBox's REST API requires tags to be referenced as {"slug": ...}
                # dicts (or numeric IDs) on create/update - NOT bare strings. Passing
                # plain strings causes a 400 Bad Request ("Related objects must be
                # referenced by numeric ID or by dictionary of attributes"). Track
                # the current set as plain slugs for easy membership checks, but
                # always convert to the {"slug": ...} dict form before writing back.
                tag_slugs = [t.slug for t in existing.tags]
                if SYNC_TAG not in tag_slugs:
                    tag_slugs.append(SYNC_TAG)
                    changed = True
                if STALE_TAG in tag_slugs:
                    tag_slugs.remove(STALE_TAG)
                    changed = True

                if changed:
                    existing.tags = [{"slug": s} for s in tag_slugs]
                    existing.save()
                    summary["updated"] += 1
                else:
                    summary["unchanged"] += 1
            else:
                nb.ipam.prefixes.create(
                    prefix=item.prefix,
                    description=description,
                    status="active",
                    tags=[{"slug": SYNC_TAG}],
                )
                summary["created"] += 1
        except Exception as e:
            log.error(f"Failed to sync prefix {item.prefix}: {e}")
            summary["failed"] += 1

    # Flag prefixes NetBox has (tagged azure-sync) that we did NOT see this run.
    # We do NOT delete them -- just tag for human review, since a transient
    # Azure API issue shouldn't cause us to lose data.
    managed_prefixes = nb.ipam.prefixes.filter(tag=SYNC_TAG)
    for p in managed_prefixes:
        if p.prefix not in seen_prefixes:
            tag_slugs = [t.slug for t in p.tags]
            if STALE_TAG not in tag_slugs:
                tag_slugs.append(STALE_TAG)
                p.tags = [{"slug": s} for s in tag_slugs]
                p.save()
                summary["stale_flagged"] += 1
                log.warning(f"Prefix {p.prefix} no longer seen in Azure - flagged for review")

    return summary


def deduplicate_discovered_prefixes(discovered: list[DiscoveredPrefix]) -> list[DiscoveredPrefix]:
    """
    If the exact same CIDR shows up more than once across VNets/subscriptions
    (rare, but possible - e.g. two VNets accidentally provisioned with the
    same address space), NetBox can only hold ONE Prefix record for that
    CIDR. Processing duplicates naively would make the last one silently win
    and flip-flop the description/tags on every run depending on Azure's
    enumeration order.

    Instead: keep the first occurrence, but log it loudly and fold the
    other sources into the description, since a duplicate CIDR is very
    likely a real network conflict worth a human looking at - not just an
    implementation detail to paper over quietly.
    """
    seen: dict[str, DiscoveredPrefix] = {}
    conflicts: dict[str, list[str]] = {}

    for item in discovered:
        source_label = f"{item.vnet_name} ({item.resource_group}, sub:{item.subscription_id})"
        if item.prefix not in seen:
            seen[item.prefix] = item
            conflicts[item.prefix] = [source_label]
        else:
            conflicts[item.prefix].append(source_label)

    result = []
    for prefix, item in seen.items():
        sources = conflicts[prefix]
        if len(sources) > 1:
            log.warning(
                f"DUPLICATE CIDR DETECTED: {prefix} appears in {len(sources)} places: "
                f"{'; '.join(sources)} - keeping the first and flagging in its description. "
                f"This usually indicates a real Azure network conflict worth reviewing."
            )
            item.vnet_name = f"{item.vnet_name} [CONFLICT: also seen in {len(sources) - 1} other VNet(s) - check logs]"
        result.append(item)

    return result


def build_hardened_netbox_session() -> "requests.Session":
    """
    pynetbox's default session has no timeout and no retry policy - a slow or
    flaky NetBox instance can hang a sync run indefinitely, or fail outright
    on a single transient blip. This wraps requests with:
      - a retry policy for 429/5xx (matches the pattern used elsewhere in
        this project for the Confluence API calls)
      - a default request timeout, since requests has none by default and a
        hung connection would otherwise block forever
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    class TimeoutHTTPAdapter(HTTPAdapter):
        def __init__(self, *args, timeout=30, **kwargs):
            self._default_timeout = timeout
            super().__init__(*args, **kwargs)

        def send(self, request, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = self._default_timeout
            return super().send(request, **kwargs)

    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        raise_on_status=False,
    )
    adapter = TimeoutHTTPAdapter(max_retries=retry, timeout=30)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def run_sync() -> int:
    """The actual Azure -> NetBox sync. Returns a process exit code."""
    subscription_ids = [
        s.strip() for s in get_env("AZURE_SUBSCRIPTION_ID").split(",") if s.strip()
    ]
    netbox_url = get_env("NETBOX_URL")
    netbox_token = get_env("NETBOX_TOKEN")

    nb = pynetbox.api(netbox_url, token=netbox_token)
    nb.http_session = build_hardened_netbox_session()  # timeout + retry, see above

    try:
        ensure_tags_exist(nb)
    except Exception as e:
        log.error(f"Could not reach NetBox to prepare tags: {e}")
        return 1

    try:
        discovered = discover_azure_prefixes(subscription_ids)
        discovered = deduplicate_discovered_prefixes(discovered)
    except Exception as e:
        log.error(f"Azure discovery failed: {e}")
        return 1

    summary = sync_to_netbox(nb, discovered)

    log.info(
        "Sync complete: "
        f"{summary['created']} created, "
        f"{summary['updated']} updated, "
        f"{summary['unchanged']} unchanged, "
        f"{summary['stale_flagged']} flagged stale, "
        f"{summary['failed']} failed"
    )

    return 2 if summary["failed"] > 0 else 0


def main():
    parser = argparse.ArgumentParser(
        description="Sync Azure VNet address-space CIDRs into NetBox, and optionally manage its own cron schedule."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a KEY=VALUE config file (needed for cron, since cron doesn't inherit your shell env). "
             "If omitted, real environment variables are used instead.",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Write a starter config file (default: ./netbox-azure-sync.env) and exit.",
    )
    parser.add_argument(
        "--install-cron",
        action="store_true",
        help="Install a daily cron job on this machine that runs this script. Requires --config.",
    )
    parser.add_argument(
        "--uninstall-cron",
        action="store_true",
        help="Remove this script's cron job from the current user's crontab, and exit.",
    )
    parser.add_argument(
        "--cron-schedule",
        default=DEFAULT_CRON_SCHEDULE,
        help=f"Cron schedule expression to install (default: '{DEFAULT_CRON_SCHEDULE}' - 03:00 daily).",
    )
    parser.add_argument(
        "--log-file",
        default="netbox-azure-sync.log",
        help="Where cron output should be redirected (default: ./netbox-azure-sync.log).",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Full VM setup: installs Docker, deploys NetBox, pulls images, creates the "
             "admin user + API token, writes the sync config, and installs the cron job. "
             "Must be run as root (sudo). See --create-azure-sp for the Azure side.",
    )
    parser.add_argument(
        "--netbox-dir",
        default="/opt/netbox-docker",
        help="Where to write the NetBox docker-compose files (default: /opt/netbox-docker).",
    )
    parser.add_argument(
        "--netbox-port",
        default="8000",
        help="Host port to expose the NetBox UI on (default: 8000).",
    )
    parser.add_argument(
        "--create-azure-sp",
        action="store_true",
        help="During --bootstrap, also create a Reader-only Azure service principal via the "
             "Azure CLI and write its credentials into the config file. Requires you to have "
             "already run 'az login' once - this is the one step that needs a human.",
    )
    parser.add_argument(
        "--azure-subscription-id",
        default=None,
        help="Required if using --create-azure-sp.",
    )
    args = parser.parse_args()

    config_path = args.config or "netbox-azure-sync.env"

    if args.bootstrap:
        bootstrap(args)
        sys.exit(0)

    if args.init_config:
        init_config(config_path)
        sys.exit(0)

    if args.uninstall_cron:
        uninstall_cron()
        sys.exit(0)

    if args.install_cron:
        if not args.config:
            log.error("--install-cron requires --config /path/to/your/config.env (cron needs an absolute, stable path).")
            sys.exit(1)
        install_cron(args.cron_schedule, args.config, args.log_file)
        sys.exit(0)

    # Normal run: sync Azure -> NetBox
    if args.config:
        load_config_file(args.config)

    sys.exit(run_sync())


if __name__ == "__main__":
    main()
