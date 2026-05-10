# Architecture

`habomero` runs a local OMERO stack with Docker Compose:

- `db`: PostgreSQL backing store
- `omero-server`: OMERO.server service
- `omero-web`: OMERO.web standalone service

Persistent data is kept in `data/`:

- `data/postgres`
- `data/omero`
- `data/omero-web-var`
- `data/backups`

Ansible is used for idempotent local prep (`ansible/playbook.yml`) and is safe on macOS because Docker installation tasks are Linux-only.
