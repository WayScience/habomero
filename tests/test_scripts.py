"""Unit tests for core script helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import safe_restart, scan_dirs, show_access_url, validate


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


def test_scan_dirs_collapses_overlapping_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested configured scan roots should collapse to the broader root."""

    project_root = tmp_path / "project"
    project_root.mkdir()
    root = project_root / "data"
    nested = root / "nested"
    nested.mkdir(parents=True)

    config_path = project_root / "scan_dirs.yml"
    config_path.write_text(
        "scan_directories:\n  - data\n  - data/nested\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)

    result = scan_dirs.load_scan_directories()
    assert result == [root.resolve()]


def test_safe_restart_compose_args_include_scan_roots_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should preserve generated scan-root compose mounts."""

    scan_roots_compose = tmp_path / "data/state/scan_roots.compose.yml"
    scan_roots_compose.parent.mkdir(parents=True)
    scan_roots_compose.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(safe_restart, "SCAN_ROOTS_COMPOSE", scan_roots_compose)

    assert safe_restart.compose_args() == [
        "docker",
        "compose",
        "-f",
        "compose.yml",
        "-f",
        str(scan_roots_compose),
    ]


def test_safe_restart_database_config_reads_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should use the same database values as Compose."""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "POSTGRES_DB=custom_omero\nPOSTGRES_USER=custom_user\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.setattr(safe_restart, "ENV_PATH", env_path)

    assert safe_restart.database_config() == ("custom_omero", "custom_user")


def test_safe_restart_removes_only_repository_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should remove OMERO repository locks without touching data."""

    repository = tmp_path / "repository"
    lock_path = repository / "uuid/.lock"
    image_path = repository / "uuid/image.tiff"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("stale", encoding="utf-8")
    image_path.write_text("pixels", encoding="utf-8")

    monkeypatch.setattr(safe_restart, "OMERO_REPOSITORY_ROOT", repository)

    removed = safe_restart.remove_stale_lock_files()

    assert removed == [lock_path]
    assert not lock_path.exists()
    assert image_path.read_text(encoding="utf-8") == "pixels"


def test_safe_restart_uses_root_helper_for_protected_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should handle lock files owned by container users."""

    class ProtectedLock:
        def unlink(self) -> None:
            raise PermissionError("denied")

    lock_path = ProtectedLock()
    protected: list[Path] = []

    def fake_root_helper(lock_paths: list[Path]) -> None:
        protected.extend(lock_paths)

    monkeypatch.setattr(safe_restart, "stale_lock_files", lambda: [lock_path])
    monkeypatch.setattr(safe_restart, "remove_locks_with_root_helper", fake_root_helper)

    removed = safe_restart.remove_stale_lock_files()

    assert removed == [lock_path]
    assert protected == [lock_path]
