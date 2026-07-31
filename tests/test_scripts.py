"""Unit tests for core script helpers."""

from __future__ import annotations

import subprocess
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


def test_scan_dirs_materializes_per_root_import_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping entries can attach an explicit import owner to a scan root."""

    project_root = tmp_path / "project"
    source = project_root / "cardiac"
    source.mkdir(parents=True)
    config_path = project_root / "scan_dirs.yml"
    state_path = project_root / "state.yml"
    compose_path = project_root / "compose.yml"
    config_path.write_text(
        "scan_directories:\n  - path: cardiac\n    import_user: habomero\n",
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
            "import_user": "habomero",
        }
    ]
    assert next(iter(mapping.values()))["import_user"] == "habomero"


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


def test_sync_users_does_not_join_shared_group_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared group visibility is opt-in for non-primary-group users."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: viewer\n"
        "    first_name: View\n"
        "    last_name: User\n"
        "    group: restricted\n"
        "    email: viewer@example.org\n"
        "    institution: Local Lab\n"
        "    password: viewer\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)

    users = sync_users.load_users()

    assert users[0]["join_shared_group"] is False


def test_sync_users_group_absence_removes_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opted-out users are removed from an old shared-group membership."""

    commands: list[str] = []

    def fake_run(root_password: str, command: str) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sync_users, "run_in_omero_with_retry", fake_run)

    sync_users.ensure_user_group_absence("root-password", "viewer", "lab")

    assert commands == ["omero user leavegroup lab --name=viewer"]


def test_sync_users_loads_scan_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-root scan groups are discovered for sync and permissions."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text(
        "scan_directories:\n"
        "  - path: a\n"
        "    group: rxrx19a\n"
        "  - path: b\n"
        "    group: cfret_subtyping_data\n"
        "scan_group_permissions: read-annotate\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "SCAN_CONFIG_PATH", config_path)

    assert sync_users.load_scan_groups() == {"rxrx19a", "cfret_subtyping_data"}
    assert sync_users.load_scan_group_permissions() == "read-annotate"


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
    keep_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/keep.tif"
    )
    missing_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/missing.tif"
    )
    other_key = import_scan.imported_file_key(
        "root_b", "habomero", "lab", "/scan/roots/root_b/other.tif"
    )
    imported = {
        keep_key,
        missing_key,
        other_key,
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
        {"root_a|owner=habomero|group=lab|root": 99},
    )

    assert deleted == 1
    assert deleted_ids == [123]
    assert imported == {
        keep_key,
        other_key,
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


def test_import_config_loads_default_import_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default import owner can be configured explicitly."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("import_user: habomero\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_default_import_user() == "habomero"


def test_import_config_loads_legacy_reimport_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy imported-file state reprocessing requires an explicit flag."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("reimport_legacy_import_state: true\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_reimport_legacy_import_state() is True


def test_import_config_loads_duplicate_project_cleanup_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Obsolete duplicate Project cleanup is enabled by default and configurable."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text(
        "cleanup_obsolete_duplicate_projects: false\n", encoding="utf-8"
    )

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_cleanup_obsolete_duplicate_projects() is False


def test_import_state_keys_include_owner_and_group() -> None:
    """Project/dataset state is scoped to avoid reusing old ownership."""

    assert (
        import_scan.state_scope_key("root_a", "habomero", "lab")
        == "root_a|owner=habomero|group=lab"
    )
    assert (
        import_scan.imported_file_key(
            "root_a",
            "habomero",
            "lab",
            "/scan/roots/root_a/image.tif",
        )
        == "root_a|owner=habomero|group=lab:/scan/roots/root_a/image.tif"
    )
    assert (
        import_scan.legacy_imported_file_key("root_a", "/scan/roots/root_a/image.tif")
        == "root_a:/scan/roots/root_a/image.tif"
    )


def test_parse_project_records() -> None:
    """Project placement rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_project_records(
        " # | Col1 | Col2 | Col3 | Col4\n"
        "---+------+-------+------+------\n"
        " 0 | 51   | scan-root :: test | 53 | habomero\n"
    )

    assert records == [
        import_scan.ProjectRecord(
            project_id=51,
            name="scan-root :: test",
            group="53",
            owner="habomero",
        )
    ]


def test_list_projects_by_name_filters_in_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate cleanup avoids HQL literals for names containing colons."""

    commands: list[str] = []

    def fake_run_as_root(command: str) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                " # | Col1 | Col2 | Col3 | Col4\n"
                "---+------+-------+------+------\n"
                " 0 | 10   | scan-root :: other | 1 | habomero\n"
                " 1 | 51   | scan-root :: test | 53 | habomero\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(import_scan, "run_as_root", fake_run_as_root)

    records = import_scan.list_projects_by_name("scan-root :: test")

    assert records == [
        import_scan.ProjectRecord(51, "scan-root :: test", "53", "habomero")
    ]
    assert "p.name =" not in commands[0]
    assert "scan-root :: test" not in commands[0]
    assert "details.group.id" in commands[0]
    assert "details.group.name" not in commands[0]


def test_root_cleanup_commands_retry_transient_session_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root cleanup commands retry when OMERO is temporarily not initialized."""

    attempts: list[str] = []

    def fake_run_as_root(command: str) -> subprocess.CompletedProcess[str]:
        attempts.append(command)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="ApiUsageException:Server not fully initialized",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(import_scan, "run_as_root", fake_run_as_root)
    monkeypatch.setattr(import_scan.time, "sleep", lambda seconds: None)

    result = import_scan.run_as_root_with_retry("hql test")

    assert result.returncode == 0
    assert attempts == ["hql test", "hql test"]


def test_cleanup_obsolete_duplicate_projects_keeps_current_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate cleanup removes same-named Projects except the configured one."""

    deleted: list[int] = []

    monkeypatch.setattr(
        import_scan,
        "list_projects_by_name",
        lambda name: [
            import_scan.ProjectRecord(
                10, "scan-root :: Way_McKinsey_Cardiac_Fibrosis", "1", "habomero"
            ),
            import_scan.ProjectRecord(
                51, "scan-root :: Way_McKinsey_Cardiac_Fibrosis", "53", "habomero"
            ),
        ],
    )
    monkeypatch.setattr(
        import_scan,
        "delete_project",
        lambda project_id: not deleted.append(project_id),
    )

    count = import_scan.cleanup_obsolete_duplicate_projects(
        "habomero",
        "way_mckinsey_cardiac_fibrosis",
        "scan-root",
        "/home/davebunten/mnt/Way_McKinsey_Cardiac_Fibrosis",
        51,
    )

    assert count == 1
    assert deleted == [10]


def test_parse_dataset_records() -> None:
    """Dataset rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_dataset_records(
        " # | Col1 | Col2\n---+------+------\n 0 | 101  | SPLAT_data :: pilot_images\n"
    )

    assert records == [
        import_scan.DatasetRecord(
            dataset_id=101,
            name="SPLAT_data :: pilot_images",
        )
    ]


def test_parse_group_records() -> None:
    """Group rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_group_records(
        " # | Col1 | Col2\n"
        "---+------+------\n"
        " 0 | 53   | way_mckinsey_cardiac_fibrosis\n"
    )

    assert records == [
        import_scan.GroupRecord(
            group_id=53,
            name="way_mckinsey_cardiac_fibrosis",
        )
    ]


def test_reconcile_project_id_prefers_configured_owner_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale local Project state is updated to configured OMERO placement."""

    configured_project_id = 51
    project_state = {"root_a|owner=habomero|group=way_mckinsey": 10}

    monkeypatch.setattr(
        import_scan,
        "list_projects_by_name",
        lambda name: [
            import_scan.ProjectRecord(10, name, "1", "habomero"),
            import_scan.ProjectRecord(configured_project_id, name, "53", "habomero"),
        ],
    )
    monkeypatch.setattr(import_scan, "group_ids_by_name", lambda group: {"53"})

    project_id = import_scan.reconcile_project_id(
        "habomero",
        "way_mckinsey",
        "root_a",
        "scan-root",
        "/mnt/Way_McKinsey_Cardiac_Fibrosis",
        10,
        project_state,
    )

    assert project_id == configured_project_id
    assert (
        project_state["root_a|owner=habomero|group=way_mckinsey"]
        == configured_project_id
    )


def test_cleanup_obsolete_duplicate_datasets_keeps_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dataset cleanup removes duplicate folder names under the kept Project."""

    deleted: list[int] = []

    monkeypatch.setattr(
        import_scan,
        "list_project_datasets",
        lambda project_id: [
            import_scan.DatasetRecord(100, "SPLAT_data :: pilot_images"),
            import_scan.DatasetRecord(101, "SPLAT_data :: pilot_images"),
            import_scan.DatasetRecord(200, "other"),
        ],
    )
    monkeypatch.setattr(
        import_scan,
        "delete_dataset_as_root",
        lambda dataset_id: not deleted.append(dataset_id),
    )

    count = import_scan.cleanup_obsolete_duplicate_datasets(51, {101})

    assert count == 1
    assert deleted == [100]


def test_current_dataset_ids_for_root_only_current_scope() -> None:
    """Current Dataset IDs come only from the configured root owner/group scope."""

    dataset_state = {
        "root_a|owner=habomero|group=way|path/a": 101,
        "root_a|owner=legacy|group=lab|path/a": 100,
        "root_b|owner=habomero|group=way|path/a": 200,
    }

    assert import_scan.current_dataset_ids_for_root(
        dataset_state,
        "root_a",
        "habomero",
        "way",
    ) == {101}


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
