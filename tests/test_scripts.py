"""Unit tests for core script helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import scan_dirs, show_access_url, validate


def test_read_env_var_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should override .env file lookup."""

    monkeypatch.setenv("OMERO_WEB_PORT", "9999")
    value = show_access_url.read_env_var("OMERO_WEB_PORT", "4080")
    assert value == "9999"


def test_scan_dirs_rejects_escape_without_external_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths outside project root are rejected unless explicitly enabled."""

    project_root = tmp_path / "project"
    project_root.mkdir()
    config_path = project_root / "scan_dirs.yml"
    config_path.write_text("scan_directories:\n  - ../outside\n", encoding="utf-8")

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="escapes project root"):
        scan_dirs.load_scan_directories()


def test_validate_layout_missing_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation should fail when required files are missing."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validate, "REQUIRED_PATHS", [Path("compose.yml")])

    with pytest.raises(FileNotFoundError, match="compose.yml"):
        validate.validate_layout()
