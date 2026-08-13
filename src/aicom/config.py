from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICOM_", env_file=".env")

    database_url: str = "postgresql+psycopg://aicom:aicom@localhost:5432/aicom"
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel: str = ""
    slack_approver_ids: tuple[str, ...] = ()
    workspace_root: Path = Path("./workspaces")
    artifact_repo_path: Path = Path("./artifacts")
    claude_binary: str = "claude"
    worker_poll_seconds: float = 2.0
    # How often the worker refreshes a run's heartbeat while it is executing,
    # independent of whether the agent has emitted any stream events. A long
    # silent run must not look stale to Task 13's sweeper.
    worker_heartbeat_seconds: float = 15.0

    # Both default to relative paths above, but every consumer (commit_run_artifacts,
    # stage_previous_reports) joins them with a run id and passes the result to `git`
    # with `cwd=repo` (or does a plain filesystem read) -- resolving a relative path
    # against whatever the process's cwd happens to be at that moment, not against this
    # setting's own intent. That silently produced `artifacts/artifacts/<run_id>` and a
    # git exit 128 the one time cwd wasn't the repo root, with `stage_previous_reports`
    # separately misreporting the same root cause as a report "missing from disk".
    # Normalising here, once, at the settings boundary, means every downstream reader
    # gets an absolute path regardless of cwd, instead of each call site having to
    # remember to resolve it (and some inevitably not).
    @field_validator("workspace_root", "artifact_repo_path", mode="after")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.resolve()


def load_settings() -> Settings:
    return Settings()
