"""Guard script that enforces execution from the project root."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def require_project_root_cwd() -> None:
    """Exit if current working directory is not the repository root."""

    cwd = Path.cwd().resolve()
    if cwd != PROJECT_ROOT:
        msg = (
            "Run poe tasks from the project root only. "
            f"Expected: {PROJECT_ROOT} | Current: {cwd}"
        )
        raise SystemExit(msg)


if __name__ == "__main__":
    require_project_root_cwd()
