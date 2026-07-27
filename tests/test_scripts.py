"""Unit tests for core script helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import (
    import_scan,
    safe_restart,
    scan_dirs,
    show_access_url,
    sync_users,
    validate,
)


def test_read_env_var_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should override .env file lookup."""

    monkeypatch.setenv("OMERO_WEB_PORT", "9999")
    value = show_access_url.read_env_var("OMERO_WEB_PORT", "4080")
    assert value == "9999"


def test_show_access_url_prints_configured_hostname(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured LAN hostnames should appear in copy/paste URLs."""

    monkeypatch.setenv("OMERO_WEB_PORT", "4080")
    monkeypatch.setenv("OMERO_PUBLIC_HOSTNAME", "habomero.local")
    monkeypatch.setattr(show_access_url, "get_local_ip", lambda: "192.0.2.10")

    show_access_url.main()

    output = capsys.readouterr().out
    assert "http://habomero.local:4080/webclient/" in output


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


def test_scan_dirs_materializes_per_root_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping entries can attach an OMERO group to a scan root."""

    project_root = tmp_path / "project"
    source = project_root / "cardiac"
    source.mkdir(parents=True)
    config_path = project_root / "scan_dirs.yml"
    state_path = project_root / "state.yml"
    compose_path = project_root / "compose.yml"
    config_path.write_text(
        "scan_directories:\n"
        "  - path: cardiac\n"
        "    group: way_mckinsey_cardiac_fibrosis\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(scan_dirs, "STATE_PATH", state_path)
    monkeypatch.setattr(scan_dirs, "COMPOSE_OVERRIDE_PATH", compose_path)

    entries = scan_dirs.load_scan_directory_entries()
    mapping = scan_dirs.materialize_scan_roots(entries)

    assert entries == [
        {
            "path": str(source.resolve()),
            "group": "way_mckinsey_cardiac_fibrosis",
        }
    ]
    assert next(iter(mapping.values()))["group"] == "way_mckinsey_cardiac_fibrosis"


def test_sync_users_loads_shared_group_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restricted users can avoid joining the global shared import group."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: way_mckinsey\n"
        "    first_name: Way\n"
        "    last_name: McKinsey\n"
        "    group: way_mckinsey_cardiac_fibrosis\n"
        "    join_shared_group: false\n"
        "    email: way@example.org\n"
        "    institution: Local Lab\n"
        "    password: way_mckinsey\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)

    users = sync_users.load_users()

    assert users[0]["join_shared_group"] is False
    assert users[0]["extra_groups"] == []


def test_sync_users_loads_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User templates can reference password environment variables."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: habomero\n"
        "    first_name: Habomero\n"
        "    last_name: Service\n"
        "    group: lab\n"
        "    email: habomero@example.org\n"
        "    institution: Local Lab\n"
        "    password_env: HABOMERO_TEST_PASSWORD\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HABOMERO_TEST_PASSWORD", "from-env")

    users = sync_users.load_users()

    assert users[0]["password"] == "from-env"


def test_import_scan_loads_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import credentials can come from password_env entries."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n  - username: habomero\n    password_env: HABOMERO_IMPORT_PASSWORD\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_scan, "USERS_CONFIG_PATH", config_path)
    monkeypatch.setenv("HABOMERO_IMPORT_PASSWORD", "import-password")

    credentials, first_username = import_scan.load_user_credentials()

    assert first_username == "habomero"
    assert credentials == {"habomero": "import-password"}


def test_delete_missing_imports_removes_deleted_image_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale source files are deleted from OMERO and removed from state."""

    state_path = tmp_path / "imported_files.txt"
    imported = {
        "root_a:/scan/roots/root_a/keep.tif",
        "root_a:/scan/roots/root_a/missing.tif",
        "root_b:/scan/roots/root_b/other.tif",
    }
    deleted_ids: list[int] = []

    monkeypatch.setattr(import_scan, "IMPORT_STATE_PATH", state_path)
    monkeypatch.setattr(
        import_scan,
        "load_dataset_image_ids_by_name",
        lambda *args: [123],
    )

    def fake_delete_image(
        owner: str,
        owner_password: str,
        shared_group: str,
        image_id: int,
    ) -> bool:
        deleted_ids.append(image_id)
        return True

    monkeypatch.setattr(import_scan, "delete_image", fake_delete_image)

    deleted = import_scan.delete_missing_imports(
        "habomero",
        "habomero",
        "lab",
        "root_a",
        "/scan/roots/root_a",
        {"/scan/roots/root_a/keep.tif"},
        imported,
        {"root_a|root": 99},
    )

    assert deleted == 1
    assert deleted_ids == [123]
    assert imported == {
        "root_a:/scan/roots/root_a/keep.tif",
        "root_b:/scan/roots/root_b/other.tif",
    }
    assert state_path.read_text(encoding="utf-8").splitlines() == sorted(imported)


def test_import_config_loads_explicit_omero_delete_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public cleanup flag names OMERO as the deletion target."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("delete_omero_missing_files: true\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    config = import_scan.load_import_config()

    assert config[-1] is True


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
