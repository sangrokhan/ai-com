from pathlib import Path

import pytest

from aicom.config import Settings


def test_relative_workspace_and_artifact_paths_are_normalised_to_absolute() -> None:
    settings = Settings(
        workspace_root=Path("./workspaces"),
        artifact_repo_path=Path("./artifacts"),
        database_url="postgresql+psycopg://unused/unused",
    )

    assert settings.workspace_root.is_absolute()
    assert settings.artifact_repo_path.is_absolute()


def test_defaults_are_already_absolute() -> None:
    # The shipped defaults are relative string literals in config.py; the
    # validator must normalise them the same way it normalises an
    # explicitly-passed relative path, or a caller that never overrides these
    # settings gets exactly the bug this test guards against.
    settings = Settings(database_url="postgresql+psycopg://unused/unused")

    assert settings.workspace_root.is_absolute()
    assert settings.artifact_repo_path.is_absolute()


def test_relative_path_resolves_against_cwd_at_construction_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        workspace_root=Path("./workspaces"),
        artifact_repo_path=Path("./artifacts"),
        database_url="postgresql+psycopg://unused/unused",
    )

    assert settings.workspace_root == (tmp_path / "workspaces").resolve()
    assert settings.artifact_repo_path == (tmp_path / "artifacts").resolve()
