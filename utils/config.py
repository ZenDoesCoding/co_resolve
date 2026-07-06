from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # GitHub Auth Settings
    github_app_id: Optional[str] = None
    github_private_key: Optional[str] = None
    github_webhook_secret: str = "default_unsafe_secret"
    github_token: Optional[str] = None # Backup for simple PAT auth
    
    # LLM Settings
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    gemini_api_key: str = ""
    llm_model: str = "gemini-3.1-flash-lite"
    llm_provider: str = "google"
    thinking_level: str = "high"
    
    # Agent Modes
    turbo_mode: bool = False
    
    # Token Limits
    max_total_tokens: int = 200000
    max_orchestrator_tokens: int = 50000
    max_sandbox_tokens: int = 10000
    max_pr_manager_tokens: int = 5000

    model_config = SettingsConfigDict(env_file=('../.env', '.env'), env_file_encoding='utf-8', extra='ignore')

settings = Settings()
