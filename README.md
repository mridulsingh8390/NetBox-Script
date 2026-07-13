# Automated Azure VNet Monitoring — NetBox

**Scope:** Azure VNet CIDRs synced automatically into NetBox as the source of truth, replacing manual tracking.

## Problem

We have a large, ever-growing private network on SCP (Azure). At cluster creation time, a private CIDR range is chosen for each vnet, picked so it doesn't overlap any other CIDR range already in use.

Today this is tracked manually in a shared document. Engineers are expected to update it whenever a vnet/resource is added or removed. In practice:

- People forget to update the page
- The page drifts from reality (stale/incorrect data)
- There's no automated way to detect CIDR overlaps before they happen
- No audit trail of who allocated what, when

We need an automated, self-updating source of truth instead of a manually maintained doc.

## What is NetBox?

[NetBox](https://github.com/netbox-community/netbox) is an open-source **IPAM** (IP Address Management) and **DCIM** (Data Center Infrastructure Management) tool, maintained by **NetBox Labs**. It's the de facto industry standard for network source-of-truth systems.

| | NetBox Community | NetBox Cloud / Enterprise |
|---|---|---|
| Cost | Free, Apache 2.0 | Paid, managed by NetBox Labs |
| Hosting | Self-hosted | Hosted / managed |
| Fit for this use case | ✅ Yes | Optional future upgrade path |

We're self-hosting the **community edition** via Docker, on a single VM.

## Design decisions and rationale

**Why a self-hosted VM + Docker, not a managed container service (e.g. Azure Container Apps/Instances)?**

A single-VM Docker deployment is the better fit for a stateful app like NetBox: it's a better-documented, more common deployment pattern for this kind of workload, cheaper than a managed container service for an always-on stack, and lowest-maintenance since everything (NetBox, Postgres, Redis) lives in one place.

**Why a custom sync script instead of NetBox Discovery?**

NetBox Discovery is a real, separate NetBox Labs product (open source, self-hostable via the `orb-agent`) for automated network/device discovery. Its discovery backends (nmap-style scanning, SNMP polling) are built for **on-prem network devices reachable directly on the network**, not for querying a cloud provider's management API — none of them talk to Azure's ARM API. **This means NetBox Discovery does not replace the custom sync script** — the script in this package (using the Azure SDK) is the correct tool for populating VNet data from Azure. NetBox Discovery would only be relevant as a separate, additional tool if there's also on-prem network gear to auto-discover via SNMP.

**Why does "IP/Ranges listing" need no extra work?**

That's built into NetBox natively (the Prefixes, IP Ranges, and IP Addresses pages). No additional development needed — once the sync script populates Prefix objects, this hierarchy and utilization view appear automatically as part of NetBox's standard IPAM UI.

## The tool: `azure_to_netbox_sync.py`

A single Python file that keeps NetBox's IPAM data in sync with real Azure VNet address spaces — no separate services, no external scheduler, no manual CIDR tracking in a shared document.

### What it does

- Discovers every VNet's address space across your Azure subscription(s) (read-only — `Reader` role is sufficient)
- Creates/updates the matching `Prefix` in NetBox, tagged `azure-sync`
- Flags (never deletes) prefixes that were previously synced but no longer exist in Azure, tagged `stale-review`, for a human to check
- Detects and logs duplicate CIDRs across VNets (a likely real conflict, not silently ignored)
- Can install/manage its own daily cron job on the same machine
- Can bootstrap a **complete NetBox deployment** on a fresh VM — Docker, the NetBox stack, admin user, API token, and this script's own cron job — in one command
- Self-installs its own Python dependencies on first run

**Scope note:** this syncs VNet-level address spaces only (`vnet.address_space.address_prefixes`). It does not currently sync individual subnet CIDRs within each VNet — that's a separate, well-defined extension if needed later.

### Quick start — everything from scratch on a fresh VM

If NetBox isn't running yet, this is the fastest path — one command sets up NetBox itself, plus the daily sync.

```bash
# Copy azure_to_netbox_sync.py to the VM, then:
sudo python3 azure_to_netbox_sync.py --bootstrap \
  --create-azure-sp --azure-subscription-id <your-subscription-id>
```

Before running this, you must have already run `az login` once on the VM — that's the one manual step that genuinely can't be automated (there's no way to create an Azure credential without authenticating first).

This single command:
1. Installs Docker Engine + Compose plugin (skips if already present)
2. Writes a NetBox `docker-compose.yml` + auto-generated secrets (nothing to fill in by hand)
3. Pulls the NetBox/Postgres/Redis images and starts the stack
4. Waits until NetBox is actually responding
5. Creates the NetBox admin user + API token automatically (retries a few times if the database is still finishing first-boot migrations)
6. Installs the Azure CLI if missing, creates a Reader-only service principal
7. Writes all of the above into a sync config file
8. Installs the daily cron job

At the end it prints the NetBox URL, the generated admin password, and confirms what's done.

If you'd rather create the Azure credentials yourself (e.g. org policy requires a specific process), omit `--create-azure-sp` — bootstrap will do everything else and tell you exactly what's left to fill in manually.

### Quick start — NetBox already exists, you just need the sync

```bash
# 1. Generate a config file
python3 azure_to_netbox_sync.py --init-config
# -> writes netbox-azure-sync.env (chmod 600), edit it with real values

# 2. Run once by hand to verify it works before trusting the schedule
python3 azure_to_netbox_sync.py --config netbox-azure-sync.env

# 3. Install the daily cron job
python3 azure_to_netbox_sync.py --install-cron --config /path/to/netbox-azure-sync.env
```

### Go-live checklist

- [ ] `NETBOX_TOKEN` has IPAM read/write access (not full admin) — ideally also Tags write access, since the script manages `azure-sync`/`stale-review` tags
- [ ] The Azure identity (managed identity or service principal) has `Reader` on every subscription you're syncing — nothing more
- [ ] Ran once manually and reviewed the summary log before scheduling it
- [ ] Cron installed and confirmed present (`crontab -l`)
- [ ] Aware that cron runs in the **server's local timezone**, not necessarily UTC (`timedatectl` to check)
- [ ] Config file confirmed `chmod 600` (contains a credential)
- [ ] Existing manual tracking document frozen/archived once NetBox is confirmed accurate

### Configuration reference

Set these either as real environment variables, or as `KEY=VALUE` lines in a config file passed via `--config` (required for cron, since cron doesn't inherit your shell's environment):

| Variable | Required | Notes |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | Yes | Comma-separate for multiple subscriptions |
| `NETBOX_URL` | Yes | e.g. `https://netbox.internal.yourcompany.com` |
| `NETBOX_TOKEN` | Yes | Scope to IPAM (+ Tags) read/write, not admin |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | No | Only needed without a managed identity |
| `SKIP_AUTO_INSTALL` | No | Set to `1` to disable the dependency self-install (e.g. a prebuilt container image) |

Real environment variables always take priority over the config file — useful for a one-off manual override without editing the file.

### CLI reference

| Command | What it does |
|---|---|
| `--bootstrap` | Full VM setup: Docker, NetBox, admin user + token, config, cron. Requires root. |
| `--bootstrap --create-azure-sp --azure-subscription-id <id>` | Same, plus auto-creates the Azure service principal (requires prior `az login`) |
| `--init-config [--config path]` | Write a starter config file with placeholder values |
| `--config path` (no other flags) | Run the sync once, using that config file |
| (no flags, real env vars set) | Run the sync once, using the shell environment |
| `--install-cron --config path [--cron-schedule "0 3 * * *"] [--log-file path]` | Install/update the daily cron job |
| `--uninstall-cron` | Remove this script's cron entry (leaves other cron jobs untouched) |

### How the sync behaves (safety properties)

- **Never deletes.** A prefix that disappears from Azure gets tagged `stale-review`, never removed — a human decides.
- **Idempotent.** Safe to re-run manually or via cron repeatedly; won't create duplicates or clobber fields it doesn't own.
- **Duplicate CIDRs are flagged, not silently resolved.** If the same CIDR appears across two VNets (a real conflict, however rare), the script keeps one, logs a loud warning naming every source, and marks it in the NetBox description for review.
- **Resilient to a slow/flaky NetBox.** API calls use a session with a 30s timeout and automatic retry on 429/5xx — a transient blip doesn't fail the whole run.
- **Resilient to first-boot races (bootstrap mode).** Creating the admin user/token retries a few times, since a truly fresh container can still be finishing database migrations even after the HTTP endpoint starts responding.

### Security notes

- The config file is `chmod 600`'d automatically since it holds a credential — but it's still plaintext on disk. Fine for a single VM; if your org needs stricter handling, populate the file from a secrets manager (e.g. Azure Key Vault) right before each cron run instead of storing values there permanently.
- The Azure identity should only ever have `Reader` — this script never writes to Azure.
- The NetBox token should be scoped to IPAM (+ Tags), not a superuser/admin token.
- If dependency auto-install (`--break-system-packages`) is blocked by your org's Python policy, create and activate a venv before running the script — dependencies will then install cleanly into it without needing that flag at all.

### Troubleshooting

**"Config file not found"** — check the path passed to `--config` is correct and absolute (cron needs an absolute path, since it doesn't run from your current directory).

**Cron job doesn't seem to run** — confirm with `crontab -l` that the entry exists, check the log file path you configured, and confirm the server's local timezone matches your expectation (`timedatectl`).

**`--bootstrap` fails at Azure SP creation** — you likely haven't run `az login` yet on that VM. This is the one step that requires interactive human authentication; run it once, then re-run `--bootstrap --create-azure-sp`.

**Dependency install fails / blocked** — see the Security notes above about using a venv instead of `--break-system-packages`.

**A CIDR I expect to see isn't in NetBox** — check the log summary from the last run (created/updated/unchanged/stale/failed counts), and check for a `DUPLICATE CIDR DETECTED` warning, which would explain why a range you expected went to a different VNet's record.

## Open questions

- Should on-prem/SNMP-based network discovery (via NetBox Discovery / `orb-agent`) be scoped as a separate follow-up? Not applicable to Azure VNet tracking, but may be worth pursuing independently if there's on-prem network gear we also want auto-documented.
- SSO integration for engineer access to NetBox?
- Do we need VRF separation, or is a flat prefix hierarchy sufficient for our use case?
- Is subnet-level CIDR tracking (not just VNet-level) needed, or is VNet-level sufficient?
- Is topology mapping (VNet peerings/NSGs/device relationships — "what's connected to what") in scope now, or a separate follow-up?

## Files in this package

| File | Purpose |
|---|---|
| `README.md` | This document |
| `azure_to_netbox_sync.py` | The single-file tool: NetBox bootstrap + Azure sync + cron management |

## References

- NetBox docs: https://docs.netbox.dev/
- NetBox Community (GitHub): https://github.com/netbox-community/netbox
- NetBox Docker deployment (pattern this script's `--bootstrap` follows): https://github.com/netbox-community/netbox-docker
- NetBox Labs: https://netboxlabs.com/
- NetBox Discovery (`orb-agent`, on-prem/SNMP discovery — not used by this tool, see "Design decisions and rationale" above): https://github.com/netboxlabs/orb-agent
