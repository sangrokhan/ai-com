from pathlib import Path

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


def load_settings() -> Settings:
    return Settings()
