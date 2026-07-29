"""Validate scan directories and materialize multi-root mounts for OMERO."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
STATE_PATH = PROJECT_ROOT / "data/state/scan_roots.yml"
COMPOSE_OVERRIDE_PATH = PROJECT_ROOT / "data/state/scan_roots.compose.yml"
RESERVED_GROUPS = {"user", "guest", "system"}


def load_scan_directory_entries() -> list[dict[str, str]]:  # noqa: C901, PLR0912, PLR0915
    """Load configured scan directories with optional per-root metadata."""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Scan directory config is missing: {CONFIG_PATH}")

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw_dirs = config.get("scan_directories", [])
    allow_external = bool(config.get("allow_external_paths", False))
    if not isinstance(raw_dirs, list):
        raise TypeError("scan_directories must be a YAML list")

    resolved: list[dict[str, str]] = []
    for item in raw_dirs:
        group = ""
        import_user = ""
        if isinstance(item, str):
            raw_path = item
        elif isinstance(item, dict):
            raw_path = item.get("path")
            raw_group = item.get("group")
            raw_import_user = item.get("import_user")
            if raw_group is not None:
                if not isinstance(raw_group, str) or not raw_group.strip():
                    raise ValueError(
                        "scan_directories group entries must be non-empty strings"
                    )
                group = raw_group.strip()
                if group in RESERVED_GROUPS:
                    raise ValueError(
                        "scan_directories group entries must be non-reserved "
                        "data groups (not one of: user, guest, system)"
                    )
            if raw_import_user is not None:
                if not isinstance(raw_import_user, str) or not raw_import_user.strip():
                    raise ValueError(
                        "scan_directories import_user entries must be non-empty strings"
                    )
                import_user = raw_import_user.strip()
        else:
            raise ValueError(
                "scan_directories entries must be non-empty strings or mappings"
            )
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("scan_directories path entries must be non-empty strings")

        entry_path = Path(raw_path).expanduser()
        target = (
            entry_path.resolve()
            if entry_path.is_absolute()
            else (PROJECT_ROOT / entry_path).resolve()
        )

        if not allow_external and PROJECT_ROOT not in [target, *target.parents]:
            raise ValueError(f"Path escapes project root: {raw_path}")
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Scan directory does not exist: {target}")

        row = {"path": str(target)}
        if group:
            row["group"] = group
        if import_user:
            row["import_user"] = import_user
        resolved.append(row)

    # De-duplicate and collapse nested paths so one file tree is scanned once.
    unique_entries = {
        Path(row["path"]): row for row in sorted(resolved, key=lambda r: r["path"])
    }
    sorted_entries = sorted(
        unique_entries.items(), key=lambda item: (len(item[0].parts), str(item[0]))
    )
    collapsed: list[dict[str, str]] = []
    collapsed_paths: list[Path] = []
    for candidate, row in sorted_entries:
        if any(parent in [candidate, *candidate.parents] for parent in collapsed_paths):
            print(f"skip-overlap: {candidate}")
            continue
        collapsed.append(row)
        collapsed_paths.append(candidate)
    return collapsed


def load_scan_directories() -> list[Path]:
    """Load configured scan directories as resolved absolute paths."""

    return [Path(row["path"]) for row in load_scan_directory_entries()]


def root_key(path: Path) -> str:
    """Generate a stable short key for a source root path."""

    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    return f"root_{digest}"


def materialize_scan_roots(
    paths: list[Path] | list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Build a root mapping and compose override for direct bind mounts."""

    mapping: dict[str, dict[str, str]] = {}
    volumes: list[str] = []

    for item in paths:
        if isinstance(item, Path):
            source = item
            group = ""
            import_user = ""
        else:
            source = Path(item["path"])
            group = item.get("group", "")
            import_user = item.get("import_user", "")
        key = root_key(source)
        container_root = f"/scan/roots/{key}"

        mapping[key] = {
            "source": str(source),
            "container_root": container_root,
        }
        if group:
            mapping[key]["group"] = group
        if import_user:
            mapping[key]["import_user"] = import_user
        volumes.append(f"{source}:{container_root}:ro")

    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        data_dir = PROJECT_ROOT / "data"
        uid = os.getuid()
        gid = os.getgid()
        fix = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "-v",
                f"{data_dir}:/data",
                "postgres:16",
                "sh",
                "-lc",
                f"chown -R {uid}:{gid} /data && chmod -R u+rwX,g+rwX /data",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if fix.returncode == 0:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        else:
            detail = fix.stderr.strip() or fix.stdout.strip() or "auto-fix failed"
            raise PermissionError(
                "Cannot write scan state directory "
                f"{STATE_PATH.parent}. Auto-fix failed: {detail}. "
                "Fix with: sudo chown -R $(id -u):$(id -g) data"
            ) from None
    STATE_PATH.write_text(yaml.safe_dump(mapping, sort_keys=True), encoding="utf-8")
    override = {"services": {"omero-server": {"volumes": volumes}}}
    COMPOSE_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_OVERRIDE_PATH.write_text(
        yaml.safe_dump(override, sort_keys=True), encoding="utf-8"
    )
    return mapping


def main() -> None:
    """Load, validate, and materialize scan roots for container mounts."""

    directories = load_scan_directory_entries()
    mapping = materialize_scan_roots(directories)
    for key, data in sorted(mapping.items()):
        print(f"{key}: {data['source']}")


if __name__ == "__main__":
    main()
