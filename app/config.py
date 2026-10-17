"""Environment-driven configuration.

All runtime settings come from environment variables so the same image/asset
runs unchanged across local, Level 1 (EC2) and later ECS environments.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "local"
    table_name: str = "IncidentTriage-local"
    aws_region: str = "us-east-1"
    dynamodb_endpoint_url: str | None = None

    bedrock_model_id: str = "amazon.nova-micro-v1:0"
    bedrock_connect_timeout_s: float = 2.0
    bedrock_read_timeout_s: float = 5.0
    bedrock_max_attempts: int = 1
    bedrock_max_output_tokens: int = 512
    bedrock_temperature: float = 0.0

    max_error_log_chars: int = 16000
    log_level: str = "INFO"
    git_sha: str = "unknown"

    metrics_enabled: bool = True
    metrics_namespace: str = "IncidentTriage"


@lru_cache
def get_settings() -> Settings:
    return Settings()
