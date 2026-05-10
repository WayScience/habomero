"""Validate and materialize configured OMERO scan directories."""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"


def load_scan_directories() -> list[Path]:
    """Load configured scan directories as project-relative paths."""

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
        if entry_path.is_absolute():
            target = entry_path.resolve()
        else:
            target = (PROJECT_ROOT / entry_path).resolve()

        if not allow_external and PROJECT_ROOT not in [target, *target.parents]:
            raise ValueError(f"Path escapes project root: {item}")

        resolved.append(target)

    return resolved


def ensure_directories(paths: list[Path]) -> None:
    """Create configured directories when they do not already exist."""

    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def main() -> None:
    """Load, validate, and create scan directories."""

    directories = load_scan_directories()
    ensure_directories(directories)
    for path in directories:
        if PROJECT_ROOT in [path, *path.parents]:
            print(path.relative_to(PROJECT_ROOT))
        else:
            print(path)


if __name__ == "__main__":
    main()
