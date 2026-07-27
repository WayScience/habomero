"""Validate local OMERO project prerequisites and configuration files."""

from __future__ import annotations

from pathlib import Path

REQUIRED_PATHS = [
    Path("compose.yml"),
    Path("ansible/playbook.yml"),
    Path(".env.example"),
    Path("config/omero/scan_dirs.yml"),
    Path("config/omero/users.example.yml"),
    Path("data"),
]


def validate_layout() -> None:
    """Fail fast when expected files or directories are missing."""

    missing = [str(item) for item in REQUIRED_PATHS if not item.exists()]
    if missing:
        missing_fmt = ", ".join(missing)
        raise FileNotFoundError(f"Missing expected path(s): {missing_fmt}")


if __name__ == "__main__":
    validate_layout()
    print("Layout validation passed")
