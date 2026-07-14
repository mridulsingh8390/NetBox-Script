# Runbook — Azure VNet Monitoring via NetBox

**System:** Self-hosted NetBox (Docker) + automated daily Azure VNet sync
**Deployed:** July 14, 2026

## 0. Fresh deployment walkthrough (from a brand-new VM)

Use this section if you're standing this up on a new VM from scratch (e.g. disaster recovery, or a second environment). Sections 1+ below assume this has already been done once.

### 0.1 Install Azure CLI

```bash
curl -fsSL 'https://azurecliprod.blob.core.windows.net/$root/deb_install.sh' | sudo bash
```

Verify:
```bash
az --version
```

### 0.2 Log into Azure

```bash
az login
```

This opens a browser (or gives you a device code if there's no browser on the VM) to authenticate interactively. **This is the one manual/human step in the entire process** — there's no way to create an Azure credential without authenticating at least once first.

Expected output (abridged):
```
A web browser has been opened at https://login.microsoftonline.com/...
Please continue the login in the web browser...
Retrieving tenants and subscriptions for the selection...

[Tenant and subscription selection]

No     Subscription name    Subscription ID                       Tenant
-----  --------------------  ------------------------------------  --------
[1]  * <your-subscription>   <your-subscription-id>                <tenant>

The subscription '<your-subscription>' is already selected.
```

Confirm you're on the right subscription:
```bash
az account show --output table
```

### 0.3 Copy the sync script to the VM

Copy `azure_to_netbox_sync.py` to wherever you want it to live (this deployment uses `/root/NetBox-Script/`):

```bash
mkdir -p /root/NetBox-Script
# copy azure_to_netbox_sync.py into that directory (scp, git clone, etc.)
cd /root/NetBox-Script
```

### 0.4 Run the full bootstrap

```bash
sudo python3 azure_to_netbox_sync.py --bootstrap \
  --create-azure-sp --azure-subscription-id <your-subscription-id>
```

This one command installs Docker, deploys NetBox, creates the admin user + API token, creates the Azure service principal, writes the sync config, and installs the daily cron job.

**Expected output, start to finish** (this is real output from this exact deployment, timestamps/IDs are from that run):

```
[bootstrap] Required Python packages not found - installing now...
[bootstrap] Dependencies installed successfully.
[bootstrap] $ systemctl enable docker
[bootstrap] $ systemctl start docker
[bootstrap] Docker installed and enabled on boot.
[bootstrap] Wrote /opt/netbox-docker/docker-compose.yml
[bootstrap] Wrote /opt/netbox-docker/netbox.env (mode 600)
[bootstrap] Pulling Docker images (netbox, postgres, redis)...
[bootstrap] $ docker compose pull
[+] pull 39/39
 ✔ Image postgres:16-alpine          Pulled                    12.2s
 ✔ Image redis:7-alpine              Pulled                     6.9s
 ✔ Image netboxcommunity/netbox:v4.1 Pulled                    19.5s
[bootstrap] Starting NetBox stack...
[bootstrap] $ docker compose up -d
[+] up 10/10
 ✔ Container netbox-docker-redis-cache-1     Started            5.8s
 ✔ Container netbox-docker-postgres-1        Started            5.9s
 ✔ Container netbox-docker-redis-1           Started            5.9s
 ✔ Container netbox-docker-netbox-1          Started            1.1s
[bootstrap] Waiting for NetBox to become ready at http://localhost:8000/ ...
```

**At this point, expect a pause of 1-4 minutes.** NetBox runs ~150+ database migrations on first boot before it starts responding to HTTP requests. This is normal, not stuck. If you want to watch it happen live, in a second terminal:

```bash
cd /opt/netbox-docker && docker compose logs netbox --tail 5
```
(run repeatedly — as long as the `Applying ...` migration names keep changing, it's healthy)

**If the wait exceeds 180 seconds**, the script exits with:
```
[bootstrap] NetBox did not become ready within 180s. Check: docker compose logs netbox
```
This is not a real failure on a slow VM — confirm migrations actually finished (`docker compose logs netbox --tail 15` should show it transition from migrations to the web server starting, and `curl -I http://localhost:8000/` should return `HTTP/1.1 302 Found`), then simply **re-run the exact same bootstrap command** — every completed step is skipped automatically, and it picks up where it left off.

Once NetBox responds, the rest completes quickly:

```
[bootstrap] NetBox is responding.
[bootstrap] Creating NetBox admin user (idempotent - skips if it already exists)...
[bootstrap] Creating/retrieving API token for admin user...
[bootstrap] Creating Azure service principal 'netbox-azure-sync' with Reader role...
[bootstrap] $ az ad sp create-for-rbac --name netbox-azure-sync --role Reader --scopes /subscriptions/<sub-id> --output json
[bootstrap] Wrote sync config to /root/NetBox-Script/netbox-azure-sync.env (mode 600)
[bootstrap] Cron job installed: 0 3 * * * /usr/bin/python3 /root/NetBox-Script/azure_to_netbox_sync.py --config /root/NetBox-Script/netbox-azure-sync.env >> /root/NetBox-Script/netbox-azure-sync.log 2>&1 # managed-by:azure_to_netbox_sync.py
[bootstrap] NOTE: this schedule runs in the server's LOCAL timezone (check `timedatectl`), not necessarily UTC.
[bootstrap] Logs will be written to: /root/NetBox-Script/netbox-azure-sync.log
======================================================================
[bootstrap] DONE.
  NetBox UI:      http://<this-vm>:8000  (user: admin / <generated-password>)
  Sync config:    /root/NetBox-Script/netbox-azure-sync.env
  Cron schedule:  0 3 * * *
======================================================================
```

**Copy the admin password from this output now** — it's only printed this once.

### 0.5 Verify by running the sync manually

Don't wait for the 3am cron to find out if it works — run it immediately:

```bash
python3 azure_to_netbox_sync.py --config netbox-azure-sync.env
```

Expected output on success (abridged — the real output also includes verbose Azure SDK request/response logging, which is normal and safe to ignore):

```
[INFO] Loaded config from netbox-azure-sync.env
[INFO] Environment is configured for ClientSecretCredential
[INFO] Scanning subscription <sub-id> ...
[INFO] DefaultAzureCredential acquired a token from EnvironmentCredential
[INFO] Discovered 1 address prefixes across Azure
[INFO] Sync complete: 1 created, 0 updated, 0 unchanged, 0 flagged stale, 0 failed
```

`1 created` on the first run is correct (nothing existed in NetBox yet). Run it again to confirm idempotency — the second run should show `0 created, 0 updated, 1 unchanged, 0 failed` instead.

### 0.6 Confirm in the NetBox UI

Browse to `http://<vm-ip>:8000/ipam/prefixes/`, log in with `admin` / the password from step 0.4, and confirm the discovered VNet CIDR(s) appear with the correct description (`<vnet-name> (<resource-group>)`).

At this point the deployment is fully verified end to end — see sections 1+ below for ongoing operation.

## 1. System overview

| Component | Location | Purpose |
|---|---|---|
| NetBox stack | `/opt/netbox-docker/` (Docker Compose: netbox, postgres, redis, redis-cache) | Source of truth UI + API |
| Sync script | `/root/NetBox-Script/azure_to_netbox_sync.py` | Pulls Azure VNet CIDRs, writes them into NetBox |
| Sync config | `/root/NetBox-Script/netbox-azure-sync.env` | Credentials + settings (mode 600) |
| Sync logs | `/root/NetBox-Script/netbox-azure-sync.log` | Output of every cron run |
| Cron schedule | `0 3 * * *` (**server local time** — check with `timedatectl`) | Runs the sync automatically, once daily |
| Backup script | `netbox_backup.sh` | Daily Postgres backup, optional Azure Blob upload — see section 5 |
| NetBox UI | `http://<vm-ip>:8000` | Login: `admin` / (password set at bootstrap — see section 4 if lost) |

**Data flow:** Azure (Reader-only) → sync script → NetBox REST API → Prefixes tagged `azure-sync`.

## 2. Routine health checks

Run these periodically (weekly is reasonable) to confirm everything's still healthy:

```bash
# 1. Are all 4 containers up?
cd /opt/netbox-docker && docker compose ps
# Expect: netbox, postgres, redis, redis-cache all "Up"

# 2. Is the cron job still installed?
crontab -l
# Expect a line ending in: # managed-by:azure_to_netbox_sync.py

# 3. Did the last scheduled sync succeed?
tail -30 /root/NetBox-Script/netbox-azure-sync.log
# Expect a line like: "Sync complete: X created, Y updated, Z unchanged, 0 failed"
# If "failed" is non-zero, see section 6 (Troubleshooting)

# 4. Spot-check NetBox UI
# Browse to http://<vm-ip>:8000/ipam/prefixes/ and confirm the prefix count
# looks right and nothing is unexpectedly tagged "stale-review"
```

## 3. Common tasks

### Run the sync manually (outside the schedule)
```bash
cd /root/NetBox-Script
python3 azure_to_netbox_sync.py --config netbox-azure-sync.env
```
Safe to run anytime — idempotent. A healthy run ends with `0 failed`.

### Check what the sync will do before it does it
There's no dry-run flag currently. To preview: check **IPAM → Prefixes** in the UI first, compare against what you expect from the Azure Portal, then run the sync and diff afterward.

### View/change the cron schedule
```bash
# View current schedule
crontab -l

# Change it (e.g. to 2am instead of 3am)
python3 /root/NetBox-Script/azure_to_netbox_sync.py \
  --install-cron --config /root/NetBox-Script/netbox-azure-sync.env \
  --cron-schedule "0 2 * * *"
# Re-running --install-cron is idempotent - it replaces the existing entry, no duplicates
```

### Temporarily disable the automated sync
```bash
python3 /root/NetBox-Script/azure_to_netbox_sync.py --uninstall-cron
```
NetBox itself keeps running; only the daily Azure pull stops. Re-enable with `--install-cron` (see above) when ready.

### Add a new engineer with NetBox access
1. Log into NetBox as `admin` → **Organization → Users** (top nav / admin menu)
2. Create a new user, assign appropriate permissions (read-only for most engineers; avoid handing out admin)
3. For programmatic/API access, generate a scoped API token for that user (not the shared admin token)

## 4. Credential recovery

### Lost the NetBox admin password
```bash
cd /opt/netbox-docker
docker compose exec netbox /opt/netbox/netbox/manage.py changepassword admin
```
Prompts interactively for a new password.

### Lost/need to rotate the NetBox API token used by the sync script
1. Log into NetBox UI as `admin`
2. **Admin → API Tokens** → find the sync's token, or create a new one (scope: IPAM + Tags read/write, not full admin)
3. Update `NETBOX_TOKEN=` in `/root/NetBox-Script/netbox-azure-sync.env`
4. Test: `python3 azure_to_netbox_sync.py --config netbox-azure-sync.env`

### Rotate the Azure service principal credentials
```bash
# Create a new client secret for the existing SP (avoids recreating the whole SP)
az ad sp credential reset --name netbox-azure-sync

# Update netbox-azure-sync.env with the new AZURE_CLIENT_SECRET value printed above
```
Then verify with a manual sync run.

## 5. Backup & recovery

**What matters:** the Postgres database (all of NetBox's data lives there).

### Automated daily backup (recommended, now available)

`netbox_backup.sh` — dumps, compresses, applies local retention, and optionally uploads to Azure Blob Storage. Same self-installing cron pattern as the sync script.

```bash
# One-time setup
chmod +x netbox_backup.sh

# Run once manually to verify it works
./netbox_backup.sh

# Install the daily cron job (default: 2am server local time)
./netbox_backup.sh --install-cron "0 2 * * *"
```

Optional: ship backups off this VM to Azure Blob Storage (recommended — a single-VM deployment has no redundancy if the disk is lost):
```bash
export AZURE_STORAGE_UPLOAD=true
export AZURE_STORAGE_ACCOUNT=<your-storage-account>
export AZURE_STORAGE_CONTAINER=netbox-backups   # optional, this is the default
./netbox_backup.sh --install-cron "0 2 * * *"
```
Set these as permanent exports (e.g. in `/etc/environment` or by editing the cron line directly) if you want the scheduled runs to also upload, not just manual ones.

Default local retention: 14 days (`RETENTION_DAYS` env var to change). Backups land in `/root/netbox-backups/` by default (`BACKUP_DIR` to change). Remove the scheduled backup with `./netbox_backup.sh --uninstall-cron`.

### Manual backup (one-off, or if you'd rather not use the script)

```bash
cd /opt/netbox-docker
docker compose exec -T postgres pg_dump -U netbox netbox | gzip > /root/netbox-backup-$(date +%F).sql.gz
```

**Restore from backup:**
```bash
cd /opt/netbox-docker
docker compose exec -T postgres psql -U netbox -d netbox < netbox-backup-YYYY-MM-DD.sql
```
Do this only against an empty/fresh database — restoring on top of live data can conflict.

## 6. Troubleshooting

This section is built from actual issues hit during this deployment — check here first before treating something as a new problem.

| Symptom | Cause | Fix |
|---|---|---|
| `No module named pip` during bootstrap | Fresh VM shipped without `python3-pip` | `sudo apt-get update && sudo apt-get install -y python3-pip`, then re-run `--bootstrap`. (Later script versions auto-handle this.) |
| `FileNotFoundError: docker` during bootstrap | Docker not yet installed, and the check itself crashed instead of detecting that | Fixed in the current script version (`_command_succeeds()` handles this safely). If seen again, confirm you're running the latest copy of the script. |
| `[bootstrap] NetBox did not become ready within 180s` | First-boot DB migrations on a fresh Postgres take longer than the wait window on some VMs | Not a real failure. Check `docker compose logs netbox --tail 15` — if migrations are still progressing, just wait, then re-run `--bootstrap` (idempotent, skips completed steps) |
| `CommandError: You must use --username with --noinput` during admin user creation | `docker compose exec` does not forward host environment variables into the container | Fixed in the current script version (uses `docker compose exec -e KEY=VALUE ...` explicitly). Confirm you're on the latest script if seen again. |
| `400 Bad Request: Related objects must be referenced by numeric ID or by dictionary of attributes... azure-sync` | NetBox's REST API requires tags as `{"slug": ...}` dicts, not bare strings | Fixed in the current script version. Confirm latest script if seen again. |
| Sync reports `X failed` | Usually a NetBox API or Azure auth issue | Run manually (`python3 azure_to_netbox_sync.py --config ...`) to see the full error, not just the summary line |
| A prefix is tagged `stale-review` unexpectedly | The sync no longer sees that VNet in Azure | Confirm in the Azure Portal whether the VNet was actually deleted/renamed. If it's a false positive (e.g. transient Azure API issue), it'll clear automatically on the next successful run once the VNet is seen again — nothing is ever auto-deleted, so no data was lost either way |
| Duplicate CIDR warning in logs (`DUPLICATE CIDR DETECTED`) | Two VNets genuinely share the same address space | This is very likely a real Azure network conflict — investigate in the Azure Portal, not a script bug |
| Cron doesn't seem to run at the expected time | Server's local timezone differs from what you assumed | `timedatectl` to check; the schedule is in local time, not UTC |
| `NETBOX_URL` unreachable from cron but works manually | Config file path issue — cron needs an absolute path | Confirm `crontab -l` shows an absolute path to both the script and `--config` file |

## 7. Security notes

- `netbox-azure-sync.env` contains a live credential — confirm it stays `chmod 600` (`ls -la` to check)
- The Azure service principal (`netbox-azure-sync`) should only ever have `Reader` role — verify periodically via `az role assignment list --assignee <appId>`
- The NetBox API token used by the sync should be scoped to IPAM (+ Tags), not the full admin token — if it currently *is* the admin token, consider creating a scoped one and rotating (see section 4)
- No SSO is currently configured — all access is local NetBox accounts

## 8. Known limitations (by design, not bugs)

- **VNet-level CIDRs only** — subnet-level CIDRs within each VNet are not synced. Extending this is a defined, separate piece of work if needed.
- **No automatic reservation workflow** — engineers manually check NetBox's "available prefixes" before writing Terraform `tfvars`; nothing reserves a range automatically. Small race-condition risk if two engineers provision simultaneously (documented, accepted tradeoff for simplicity).
- **No topology mapping** — NetBox tracks CIDRs, not VNet peerings/NSGs/"what's connected to what." Out of scope for this deployment as built.
- **Single VM, no HA** — if this VM is lost entirely (not just the disk), NetBox goes down until redeployed. `netbox_backup.sh` (section 5) mitigates data loss if you've enabled the Azure Blob upload option — confirm that's actually turned on, since it's opt-in, not default.

