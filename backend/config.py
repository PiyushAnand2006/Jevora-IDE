"""Application settings (pydantic-settings).

Every value can be overridden with an environment variable of the same name
(case-insensitive) or a `.env` file in the working directory. Complex values
(lists) use JSON, e.g. ``SCREEN_ALLOWED_DOMAINS='["localhost","127.0.0.1"]'``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.agents.prompts import DEFAULT_ACTING_BRAIN_SYSTEM_PROMPT

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- general -----------------------------------------------------------
    app_name: str = "Jevora IDE"
    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    data_dir: Path = PROJECT_ROOT / ".jevora-data"
    database_url: str | None = None
    frontend_dir: Path = PROJECT_ROOT / "frontend"
    workspace_root: Path = PROJECT_ROOT / "workspace"
    vault_path: Path | None = None
    plugins_dir: Path | None = None
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"]
    )
    public_base_url: str = "http://127.0.0.1:8000"
    acting_brain_system_prompt: str = Field(default=DEFAULT_ACTING_BRAIN_SYSTEM_PROMPT, max_length=20_000)

    # ---- auth ----------------------------------------------------------------
    auth_required: bool = False
    jwt_secret: SecretStr | None = None
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    login_rate_limit_per_minute: int = 5
    login_rate_limit_per_ip_per_minute: int = 30
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    github_auth_url: str = "https://github.com/login/oauth/authorize"
    github_token_url: str = "https://github.com/login/oauth/access_token"
    github_api_url: str = "https://api.github.com"

    # ---- secrets ---------------------------------------------------------------
    use_keyring: bool = True
    jevora_fernet_key: SecretStr | None = None

    # ---- providers / router --------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_autodiscover: bool = True
    provider_timeout_s: float = 120.0
    provider_models_timeout_s: float = 10.0
    provider_max_retries: int = 3
    provider_backoff_base_s: float = 0.5
    provider_backoff_cap_s: float = 20.0
    provider_max_concurrency: int = 4
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_s: float = 30.0
    json_repair_attempts: int = 2

    # ---- orchestration -----------------------------------------------------------
    escalation_threshold: float = 0.65
    iteration_ceiling: int = 6
    node_retry_cap: int = 3
    max_subagent_depth: int = 4
    max_fanout: int = 5
    max_concurrent_runs: int = 4
    max_agent_steps: int = 24
    run_token_budget: int = 600_000
    context_window_tokens: int = Field(default=262_144, ge=1)
    run_time_budget_s: int = 1800
    run_cost_budget_usd: float = 5.0
    max_prompt_chars: int = 100_000

    # ---- sandbox --------------------------------------------------------------------
    sandbox_mode: Literal["auto", "docker", "local"] = "auto"
    local_unsafe_exec: bool = False
    sandbox_image: str = "jevora-sandbox:latest"
    sandbox_base_image: str = "python:3.11-slim"
    sandbox_runtime: str | None = None
    sandbox_cpus: float = 1.0
    sandbox_memory: str = "1g"
    sandbox_pids_limit: int = 256
    sandbox_network: bool = False
    sandbox_network_allowlist: list[str] = Field(default_factory=list)
    command_timeout_s: int = 300
    approval_policy: Literal["auto", "ask_on_risk", "always_ask"] = "ask_on_risk"
    approval_timeout_s: int = 600

    # ---- decision layer -------------------------------------------------------------
    laya_enabled: bool = True
    laya_model_id: str = "convaiinnovations/laya"
    laya_device: str | None = None
    laya_preload: bool = False
    jev_api_url: str | None = None
    jev_api_key: SecretStr | None = None

    # ---- features -------------------------------------------------------------------
    web_search_enabled: bool = False
    web_search_provider: Literal["tavily", "brave", "searxng"] = "searxng"
    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None
    searxng_url: str | None = None
    max_upload_mb: int = 25
    max_attachment_text_chars: int = 200_000

    # ---- screen control ---------------------------------------------------------------
    screen_browser: Literal["local", "docker"] = "local"
    screen_allowed_domains: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    screen_max_steps: int = 25
    screen_viewport_width: int = 1280
    screen_viewport_height: int = 800
    desktop_control_enabled: bool = False
    playwright_server_image: str = "mcr.microsoft.com/playwright:v1.55.0-noble"

    @field_validator("data_dir", "workspace_root", "frontend_dir", mode="after")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("vault_path", "plugins_dir", mode="after")
    @classmethod
    def _absolute_optional(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    # ---- derived -------------------------------------------------------------------
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'jevora.db').as_posix()}"

    @property
    def resolved_vault_path(self) -> Path:
        return self.vault_path or (PROJECT_ROOT / "vault")

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def oauth_configured(self) -> dict[str, bool]:
        return {
            "google": bool(self.google_client_id and self.google_client_secret),
            "github": bool(self.github_client_id and self.github_client_secret),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
