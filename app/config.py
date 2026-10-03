from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
ROOT = Path(__file__).resolve().parent.parent

# Approximate USD per 1M tokens (input, output) from the Gemini pricing page,
# checked 2026-09-30. Unknown models report cost as null, never as zero.
MODEL_PRICES = {
    'gemini-3.1-flash-lite': (0.25, 1.50),
    'gemini-3.5-flash-lite': (0.30, 2.50),
    'gemini-3.6-flash': (0.75, 3.75),
    'gemini-3.7-flash': (0.75, 3.75),
    'gemini-3.8-flash': (0.75, 3.75),
    'gemini-3.5-flash': (1.50, 9.00),
    'gemini-2.5-flash-lite': (0.10, 0.40),
    'gemini-2.5-flash': (0.30, 2.50),
}

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    host: str = '127.0.0.1'
    port: int = 8000
    model_provider: str = 'unconfigured'
    model_name: str = ''
    # Comma-separated models offered in the UI selector (must include MODEL_NAME).
    available_models: str = ''
    # Comma-separated models tried in order when the selected model hits a quota/outage.
    model_fallbacks: str = ''
    gemini_api_key: str = Field(default='', repr=False)
    # Gemini 3.x accepts minimal/low/medium/high; empty sends no thinking config.
    gemini_thinking_level: str = 'minimal'
    max_steps: int = Field(default=6, ge=1, le=6)
    max_tool_retries: int = Field(default=2, ge=0, le=2)
    max_decision_repairs: int = Field(default=2, ge=0, le=2)
    max_output_tokens: int = Field(default=768, ge=128, le=4096)
    max_run_tokens: int = Field(default=40000, ge=1000)
    max_history_messages: int = Field(default=12, ge=0, le=12)
    run_timeout_seconds: float = Field(default=40, gt=0, le=40)
    model_timeout_seconds: float = Field(default=15, gt=0, le=30)
    tool_timeout_seconds: float = Field(default=2, gt=0, le=10)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    chat_requests_per_minute: int = Field(default=12, ge=1)
    max_daily_runs: int = Field(default=2000, ge=1)

    def models(self) -> list[str]:
        """Models the service may use; empty when no provider key is configured."""
        if self.model_provider != 'gemini' or not self.gemini_api_key or not self.model_name:
            return []
        names = [m.strip() for m in self.available_models.split(',') if m.strip()]
        return list(dict.fromkeys([self.model_name, *names]))

    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.model_fallbacks.split(',') if m.strip()]

settings = Settings()
