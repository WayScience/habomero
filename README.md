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
    A[Operator] --> B[uv run poe run]

    B --> C[scan-dirs]
    C --> C1[config/omero/scan_dirs.yml]
    C --> C2[Create/validate scan paths]

    B --> D[provision]
    D --> D1[Ansible playbook]
    D1 --> D2[data/postgres]
    D1 --> D3[data/omero]
    D1 --> D4[data/backups]
    D1 --> D5[data/omero-web-var]

    B --> E[up]
    E --> F[(Postgres)]
    E --> G[OMERO.server]
    E --> H[OMERO.web]
    F --> G
    G --> H

    B --> I[sync-users]
    I --> I1[config/omero/users.yml]
    I --> G

    B --> J[healthcheck]
    J --> K[Service state output]

    L[Browser :4080/webclient] --> H
    M[OMERO scan mount] --> G
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
You can route ownership by directory prefix using `path_user_routes` in
`config/omero/scan_dirs.yml`; each directory bucket is imported into a
dedicated Dataset automatically.

- Sync allowlisted OMERO users:

```bash
uv run poe sync-users
```

- Backup database:

```bash
uv run poe backup
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
