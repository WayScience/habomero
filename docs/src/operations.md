# Operations

## Bring up

```bash
cp .env.example .env
cp config/omero/users.example.yml config/omero/users.yml
vim .env
vim config/omero/users.yml
vim config/omero/scan_dirs.yml
uv run poe preflight
uv run poe scan-dirs
uv run poe provision
uv run poe up
uv run poe sync-users
```

`config/omero/users.yml` is local-only and ignored by Git. Store real OMERO
user passwords in `.env` and reference them with `password_env` entries. Every
configured scan directory must exist locally before `scan-dirs` runs; mount SMB
shares at the paths listed in `config/omero/scan_dirs.yml`.

## Configure scan directories

Edit `config/omero/scan_dirs.yml` and keep entries project-relative (for example: `data/inbox`).
Then apply and materialize paths:

```bash
uv run poe scan-dirs
```

## Check health and logs

```bash
uv run poe healthcheck
uv run poe logs
```

All `poe` tasks must be run from the project root directory.
`up` and the main `remote-run*` tasks also run `preflight` automatically.

## LAN hostname setup

The Docker stack exposes OMERO.web on the Linux host port configured by
`OMERO_WEB_PORT`. To make a LAN URL such as `habomero.local` work, configure
hostname resolution on the host or network.

For mDNS on a Linux server:

```bash
sudo hostnamectl set-hostname habomero
sudo apt-get update
sudo apt-get install -y avahi-daemon
sudo systemctl enable --now avahi-daemon
sudo ufw allow 4080/tcp
sudo ufw allow 5353/udp
```

Set the hostname printed by habomero in `.env`:

```bash
OMERO_PUBLIC_HOSTNAME=habomero.local
```

Most macOS/Linux clients can then use
`http://habomero.local:${OMERO_WEB_PORT:-4080}/webclient/`. For bare
`habomero`, configure router/DHCP DNS or add a hosts-file entry on each client:

```text
192.168.1.50 habomero
```

Confirm the URLs:

```bash
uv run poe show-url
```

## Safe restart without deleting data

Use this when recovering an existing OMERO stack after an unclean shutdown or
stale repository lock warning. It preserves the existing database and OMERO
repository data, creates a timestamped PostgreSQL backup under `data/backups`,
stops only the OMERO application services, removes stale repository `.lock`
files, and starts the stack again.

```bash
uv run poe safe-restart
uv run poe healthcheck
```

This task does not remove Docker volumes, wipe `data/postgres`, or wipe
`data/omero`.

## Continuous production ingest (full dataset)

Start stack and run full-dataset parallel import:

```bash
uv run poe remote-run-parallel-full
```

This command now starts continuous periodic rescans/imports automatically.
You can still run the ingest loop directly:

```bash
uv run poe import-remote-safe-continuous-parallel-full
```

Key settings in `config/omero/scan_dirs.yml`:

- `safe_import_rounds`: rounds per cycle
- `safe_import_pause_seconds`: pause between rounds in a cycle
- `safe_import_stagnant_rounds`: stop cycle after stagnant progress
- `safe_import_cycle_pause_seconds`: pause between cycles (default `300`)
- `safe_import_continuous`: whether safe-import loops continuously by config

Parallelism can be tuned at runtime:

```bash
IMPORT_WORKERS=4 uv run poe import-remote-safe-continuous-parallel-full
```

## Configure user access allowlist

Edit the local-only `config/omero/users.yml` and define approved user accounts.
Use `password_env` entries and set the real password values in `.env`.
Then synchronize those users into OMERO:

```bash
uv run poe sync-users
```

Configure each real data root with its own per-root `group` so images are not
shared with all users by default. Imports without a per-root `group` use
`shared_group` when it is configured in `config/omero/scan_dirs.yml`. Users are
not joined to that group by default; set `join_shared_group: true` only for
users who should see fallback shared content. Use `extra_groups` for
service/import users that need access to per-root import groups.
`sync-users` creates scan root groups and applies `scan_group_permissions`.
Set top-level `import_user: habomero` to keep imported projects owned by one
service account in OMERO.web. Use per-root `group` values for access control;
only use per-root `import_user` when that root should appear under a different
OMERO owner.
With `reimport_legacy_import_state: true`, files tracked by an older owner/group
state format are rechecked and imported into the current configured owner/group.
This repopulates the desired OMERO location after config changes; it does not
delete old OMERO objects that were already imported elsewhere.

## Backup

```bash
uv run poe backup
```

## Spin down

Standard spin-down:

```bash
uv run poe down
```

Deep clean spin-down (destructive):

```bash
docker compose down --volumes --remove-orphans
rm -rf data/postgres data/omero data/omero-web-var
```
