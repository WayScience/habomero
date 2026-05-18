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


def load_scan_directories() -> list[Path]:
    """Load configured scan directories as resolved absolute paths."""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Scan directory config is missing: {CONFIG_PATH}")

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw_dirs = config.get("scan_directories", [])
    allow_external = bool(config.get("allow_external_paths", False))
    if not isinstance(raw_dirs, list):
        raise TypeError("scan_directories must be a YAML list")

    resolved: list[Path] = []
    for item in raw_dirs:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("scan_directories entries must be non-empty strings")

        entry_path = Path(item).expanduser()
        target = (
            entry_path.resolve()
            if entry_path.is_absolute()
            else (PROJECT_ROOT / entry_path).resolve()
        )

        if not allow_external and PROJECT_ROOT not in [target, *target.parents]:
            raise ValueError(f"Path escapes project root: {item}")
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Scan directory does not exist: {target}")

        resolved.append(target)

    # De-duplicate and collapse nested paths so one file tree is scanned once.
    unique_paths = sorted(set(resolved), key=lambda p: (len(p.parts), str(p)))
    collapsed: list[Path] = []
    for candidate in unique_paths:
        if any(parent in [candidate, *candidate.parents] for parent in collapsed):
            print(f"skip-overlap: {candidate}")
            continue
        collapsed.append(candidate)
    return collapsed


def root_key(path: Path) -> str:
    """Generate a stable short key for a source root path."""

    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    return f"root_{digest}"


def materialize_scan_roots(paths: list[Path]) -> dict[str, dict[str, str]]:
    """Build a root mapping and compose override for direct bind mounts."""

    mapping: dict[str, dict[str, str]] = {}
    volumes: list[str] = []

    for source in paths:
        key = root_key(source)
        container_root = f"/scan/roots/{key}"

        mapping[key] = {
            "source": str(source),
            "container_root": container_root,
        }
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

    directories = load_scan_directories()
    mapping = materialize_scan_roots(directories)
    for key, data in sorted(mapping.items()):
        print(f"{key}: {data['source']}")


if __name__ == "__main__":
    main()
