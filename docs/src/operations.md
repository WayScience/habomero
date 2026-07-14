# Operations

## Bring up

```bash
cp .env.example .env
uv run poe preflight
uv run poe scan-dirs
uv run poe provision
uv run poe up
uv run poe sync-users
```

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

Edit `config/omero/users.yml` and define approved user accounts.
Then synchronize those users into OMERO:

```bash
uv run poe sync-users
```

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
