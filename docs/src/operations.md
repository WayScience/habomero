# Operations

## Bring up

```bash
cp .env.example .env
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
