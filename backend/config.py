"""Application configuration loaded from .env files."""

import os
import sys
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

if getattr(sys, "frozen", False):
    _base_dir = Path(sys._MEIPASS)
else:
    _base_dir = Path(__file__).resolve().parent.parent

_env_name = os.environ.get("BURNRATE_ENV", "development")
_env_file_path = os.environ.get("BURNRATE_ENV_FILE", str(_base_dir / f".env.{_env_name}"))

class Settings(BaseSettings):
    burnrate_env: str = "development"
    burnrate_data_dir: str | None = None
    burnrate_static_dir: str = ""
    burnrate_port: int = 8000
    burnrate_homebrew: str | None = None
    burnrate_oauth_fernet_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=_env_file_path,
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()

# Bank password format hints (for documentation)
# HDFC: UPPERCASE first 4 letters of name + DDMM (DOB) OR UPPERCASE first 4 + last 4 digits of card
# ICICI: lowercase first 4 letters of name + DDMM (DOB)
# Axis: UPPERCASE first 4 letters of name + DDMM (DOB)
