from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_api_secret: str
    tts_source_wav: str = "/data/morshu.wav"
    log_level: str = "INFO"
    tts_max_text_length: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
