
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

logger: Final[logging.Logger] = logging.getLogger(__name__)

PBKDF2_ITERATIONS: Final[int] = 240_000
SESSION_COOKIE_NAME: Final[str] = "nutrition_session"
SESSION_TTL_SECONDS: Final[int] = 14 * 24 * 3600

ROLE_ADMIN: Final[str] = "admin"
ROLE_USER: Final[str] = "user"


@dataclass(frozen=True)
class Account:
    login: str
    role: str
    tg_user_id: Optional[int]

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def normalize_login(raw_login: str) -> str:
    """`@Vasya ` → `vasya`. Логин — это TG-username без собачки."""
    return raw_login.strip().lstrip("@").lower()


def hash_password(password: str) -> str:
    """PBKDF2-хэш в формате pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>."""
    salt: bytes = secrets.token_bytes(16)
    digest: bytes = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    salt_part: str = base64.b64encode(salt).decode("ascii")
    hash_part: str = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_part}${hash_part}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_part, hash_part = stored_hash.split("$")
    except ValueError:
        logger.warning("Повреждённый хэш пароля в БД")
        return False
    if algorithm != "pbkdf2_sha256":
        logger.warning("Неизвестный алгоритм хэша: %s", algorithm)
        return False

    try:
        iterations: int = int(iterations_raw)
        salt: bytes = base64.b64decode(salt_part)
        expected: bytes = base64.b64decode(hash_part)
    except ValueError:
        logger.warning("Не удалось разобрать хэш пароля")
        return False

    actual: bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def load_or_create_secret_key(db_path: Path) -> bytes:
    key_path: Path = db_path.parent / "session.key"
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        pass
    except PermissionError as error:
        raise RuntimeError(f"нет прав на чтение {key_path}: {error}") from error

    key: bytes = secrets.token_bytes(32)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
    except PermissionError as error:
        raise RuntimeError(f"нет прав на запись {key_path}: {error}") from error
    except OSError as error:
        raise RuntimeError(f"не удалось создать {key_path}: {error}") from error

    try:
        key_path.chmod(0o600)
    except (PermissionError, NotImplementedError, OSError):
        # На части файловых систем Windows chmod — no-op; это не критично.
        logger.debug("chmod для %s не применён", key_path)
    return key


def _sign(secret_key: bytes, payload: str) -> str:
    signature: bytes = hmac.new(secret_key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def create_session_token(secret_key: bytes, login: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Подписанный токен `login.expires.signature`."""
    expires_at: int = int(time.time()) + ttl_seconds
    payload: str = f"{login}.{expires_at}"
    return f"{payload}.{_sign(secret_key, payload)}"


def read_session_token(secret_key: bytes, token: str) -> Optional[str]:
    """Вернуть login, если подпись верна и срок не истёк, иначе None."""
    try:
        login, expires_raw, signature = token.rsplit(".", 2)
    except ValueError:
        return None

    payload: str = f"{login}.{expires_raw}"
    if not hmac.compare_digest(_sign(secret_key, payload), signature):
        return None

    try:
        expires_at: int = int(expires_raw)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    return login


def generate_password(length: int = 16) -> str:
    alphabet: str = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_secure_cookie_env() -> bool:
    """Флаг Secure для куки: включается только при HTTPS (WEB_HTTPS=true)."""
    return os.environ.get("WEB_HTTPS", "").strip().lower() in {"1", "true", "yes", "on"}
