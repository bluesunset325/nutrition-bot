
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
DEFAULT_DB_PATH: Final[Path] = BASE_DIR / "data" / "nutrition.sqlite3"


def _load_dotenv(dotenv_path: Path) -> None:
    try:
        raw_text: str = dotenv_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except PermissionError:
        return
    except UnicodeDecodeError:
        return

    for line in raw_text.splitlines():
        stripped: str = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_str(name: str, default: str) -> str:
    value: Optional[str] = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def _env_float(name: str, default: float) -> float:
    raw: Optional[str] = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw: Optional[str] = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    db_path: Path
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    llm_enabled: bool
    web_host: str
    web_port: int
    use_threaded_resolver: bool


def load_config() -> AppConfig:
    _load_dotenv(BASE_DIR / ".env")
    _load_dotenv(BASE_DIR.parent / ".env")

    db_path_raw: str = _env_str("NUTRITION_DB_PATH", str(DEFAULT_DB_PATH))
    db_path: Path = Path(db_path_raw).expanduser().resolve()

    try:
        web_port: int = int(_env_str("WEB_PORT", "8000"))
    except ValueError:
        web_port = 8000

    return AppConfig(
        bot_token=_env_str("BOT_TOKEN", ""),
        db_path=db_path,
        llm_base_url=_env_str("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_model=_env_str("LLM_MODEL", "qwen2.5:7b-instruct"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 60.0),
        llm_enabled=_env_bool("LLM_ENABLED", True),
        web_host=_env_str("WEB_HOST", "127.0.0.1"),
        web_port=web_port,
        use_threaded_resolver=_env_bool("USE_THREADED_RESOLVER", True),
    )
