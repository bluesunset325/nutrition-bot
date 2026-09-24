"""Управление аккаунтами дашборда.

    python -m nutrition_bot.accounts add @vasya --admin
    python -m nutrition_bot.accounts list
    python -m nutrition_bot.accounts passwd @vasya
    python -m nutrition_bot.accounts role @vasya user
    python -m nutrition_bot.accounts delete @vasya

Пароль вводится скрытым вводом (getpass) и нигде не отображается и не пишется
в лог — в базу попадает только PBKDF2-хэш.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Any, Dict, List, Optional

from nutrition_bot.auth import (
    ROLE_ADMIN,
    ROLE_USER,
    generate_password,
    hash_password,
    normalize_login,
)
from nutrition_bot.config import AppConfig, load_config
from nutrition_bot.database import Database

MIN_PASSWORD_LENGTH: int = 8


def _prompt_password(login: str) -> Optional[str]:
    print(f"Пример надёжного пароля: {generate_password()}")
    try:
        first: str = getpass.getpass(f"Пароль для «{login}»: ")
        second: str = getpass.getpass("Повторите пароль: ")
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено.")
        return None

    if first != second:
        print("Пароли не совпадают.")
        return None
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"Пароль короче {MIN_PASSWORD_LENGTH} символов.")
        return None
    return first


async def _command_add(database: Database, raw_login: str, is_admin: bool) -> int:
    login: str = normalize_login(raw_login)
    if not login:
        print("Пустой логин.")
        return 1
    if await database.get_account(login) is not None:
        print(f"Аккаунт «{login}» уже существует. Смена пароля: passwd {login}")
        return 1

    password: Optional[str] = _prompt_password(login)
    if password is None:
        return 1

    tg_user_id: Optional[int] = await database.find_user_id_by_username(login)
    role: str = ROLE_ADMIN if is_admin else ROLE_USER
    await database.create_account(login, hash_password(password), role, tg_user_id)

    print(f"Создан аккаунт «{login}», роль: {role}")
    if tg_user_id is None:
        print(
            "Внимание: пользователь с таким @username ещё не писал боту, "
            "поэтому данные пока не привязаны. Привязка произойдёт автоматически "
            "после первого сообщения боту."
        )
    else:
        print(f"Привязан к Telegram user_id {tg_user_id}")
    return 0


async def _command_list(database: Database) -> int:
    accounts: List[Dict[str, Any]] = await database.list_accounts()
    if not accounts:
        print("Аккаунтов нет. Создай первый: add @твой_username --admin")
        return 0
    print(f"{'логин':<24}{'роль':<10}{'tg_user_id':<14}создан")
    for account in accounts:
        tg_value: str = str(account["tg_user_id"] or "—")
        print(f"{account['login']:<24}{account['role']:<10}{tg_value:<14}{account['created_at']}")
    return 0


async def _command_passwd(database: Database, raw_login: str) -> int:
    login: str = normalize_login(raw_login)
    if await database.get_account(login) is None:
        print(f"Аккаунт «{login}» не найден.")
        return 1
    password: Optional[str] = _prompt_password(login)
    if password is None:
        return 1
    await database.set_account_password(login, hash_password(password))
    print(f"Пароль для «{login}» обновлён.")
    return 0


async def _command_role(database: Database, raw_login: str, role: str) -> int:
    login: str = normalize_login(raw_login)
    if role not in {ROLE_ADMIN, ROLE_USER}:
        print(f"Роль должна быть {ROLE_ADMIN} или {ROLE_USER}.")
        return 1
    if not await database.set_account_role(login, role):
        print(f"Аккаунт «{login}» не найден.")
        return 1
    print(f"Роль «{login}» теперь {role}.")
    return 0


async def _command_delete(database: Database, raw_login: str) -> int:
    login: str = normalize_login(raw_login)
    if not await database.delete_account(login):
        print(f"Аккаунт «{login}» не найден.")
        return 1
    print(f"Аккаунт «{login}» удалён. Данные в дневнике не тронуты.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nutrition_bot.accounts",
        description="Аккаунты веб-дашборда. Логин — это @username из Telegram.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_add = subparsers.add_parser("add", help="создать аккаунт")
    parser_add.add_argument("login", help="@username из Telegram")
    parser_add.add_argument(
        "--admin", action="store_true", help="главный аккаунт: видит данные всех"
    )

    subparsers.add_parser("list", help="список аккаунтов")

    parser_passwd = subparsers.add_parser("passwd", help="сменить пароль")
    parser_passwd.add_argument("login")

    parser_role = subparsers.add_parser("role", help="сменить роль")
    parser_role.add_argument("login")
    parser_role.add_argument("role", choices=[ROLE_ADMIN, ROLE_USER])

    parser_delete = subparsers.add_parser("delete", help="удалить аккаунт")
    parser_delete.add_argument("login")

    return parser


async def main() -> int:
    arguments = _build_parser().parse_args()
    config: AppConfig = load_config()
    database: Database = Database(config.db_path)
    await database.connect()

    try:
        if arguments.command == "add":
            return await _command_add(database, arguments.login, arguments.admin)
        if arguments.command == "list":
            return await _command_list(database)
        if arguments.command == "passwd":
            return await _command_passwd(database, arguments.login)
        if arguments.command == "role":
            return await _command_role(database, arguments.login, arguments.role)
        if arguments.command == "delete":
            return await _command_delete(database, arguments.login)
        return 1
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(1)
