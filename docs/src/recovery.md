# Recovery

## Restore from backup

1. Ensure stack is running (`uv run poe up`).
1. Restore SQL dump:

```bash
uv run poe restore -- data/backups/<dump-file>.sql
```

3. Verify service health:

```bash
uv run poe healthcheck
```

## Failure-mode notes

- If restore fails because schema objects already exist, drop/recreate the target database before replaying the dump.
- If OMERO services fail to connect after restore, restart stack with `uv run poe down && uv run poe up`.
