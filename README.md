# habomero

Reproducible OMERO local-server deployment for development and pre-production testing.

## What this repo provides

- Docker Compose stack for `postgres`, `omero-server`, and `omero-web`
- Ansible-based local provisioning of runtime directories
- `uv` + `poe` operational commands
- Backup/restore and validation scripts
- Pre-commit linting for Python, YAML, Docker, shell, and Ansible

## High-level flow

```mermaid
flowchart TD
    A[You run uv run poe run] --> B[Prepare local folders]
    B --> C[Start OMERO services]
    C --> D[Sync users and import images]
    D --> E[Quick health check]
    E --> F[Open OMERO.web]

    G[scan_dirs.yml + users.yml] --> D
    H[Source image directories] --> D
    I[Browser at /webclient] --> F
```

## Quick start (macOS and Linux)

1. Install prerequisites:
   - Docker (Docker Desktop on macOS)
   - `uv`
1. Install project dependencies:

```bash
uv sync --group dev
```

3. Create environment file:

```bash
cp .env.example .env
```

4. Configure scan directories (project-relative only):

```bash
vim config/omero/scan_dirs.yml
uv run poe scan-dirs
```

5. Prepare local directories:

```bash
uv run poe provision
```

6. Start stack:

```bash
uv run poe up
```

`poe up` prints both localhost and local-network URLs for copy/paste sharing.

7. Configure user access allowlist:

```bash
vim config/omero/users.yml
uv run poe sync-users
```

8. Validate and inspect:

```bash
uv run poe validate
uv run poe healthcheck
uv run poe logs
```

OMERO.web is exposed at `http://localhost:${OMERO_WEB_PORT:-4080}`.

All `poe` operations are restricted to project-root execution and fail if run from another directory.

## Operations

- One-command local run:

```bash
uv run poe run
```

`poe run` now also auto-imports new `.tif`/`.tiff` files from the mounted
scan directory (`OMERO_SCAN_DIR` -> `/scan/inbox`) using the first user in
`config/omero/users.yml`.
Imports mirror directory hierarchy in OMERO Folders (folders map to folders,
images map to images). If `shared_group` is set in `config/omero/scan_dirs.yml`,
all configured users are joined to that group and imported content is visible
to all group members. Set `import_mode: inplace` in
`config/omero/scan_dirs.yml` to avoid duplicate storage by importing file
references instead of copying pixel data.

- Production-style full-dataset parallel ingest with periodic rescan:

```bash
uv run poe remote-run-parallel-full
```

`remote-run-parallel-full` performs startup (`scan-dirs`, `up`, `sync-users`,
`healthcheck`) and then starts continuous full-dataset safe import in parallel
mode. It keeps rescanning and importing indefinitely. Default pause between
cycles is 300 seconds and is controlled by `safe_import_cycle_pause_seconds` in
`config/omero/scan_dirs.yml`.

- Sync allowlisted OMERO users:

```bash
uv run poe sync-users
```

- Backup database:

```bash
uv run poe backup
```

- Safe recovery restart without deleting existing OMERO data:

```bash
uv run poe safe-restart
uv run poe healthcheck
```

- Restore database from dump:

```bash
uv run poe restore -- data/backups/<dump-file>.sql
```

## Spin down instructions

Orderly spin-down (keeps data volumes):

```bash
uv run poe down
```

Full teardown (removes containers, networks, and project volumes):

```bash
docker compose down --volumes --remove-orphans
```

Optional cleanup of runtime data directories (destructive):

```bash
rm -rf data/postgres data/omero data/omero-web-var
```

## Linting and tests

- Run all pre-commit hooks:

```bash
uv run pre-commit run --all-files
```

- Run tests:

```bash
uv run pytest
```

## Documentation

- [Architecture](docs/src/architecture.md)
- [Operations](docs/src/operations.md)
- [Recovery](docs/src/recovery.md)
