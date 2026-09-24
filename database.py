
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosqlite

logger: logging.Logger = logging.getLogger(__name__)

# (таблица, колонка, DDL) — применяется к базам, созданным более ранней версией.
_COLUMN_MIGRATIONS: Sequence[Tuple[str, str, str]] = (
    ("meals", "description", "description text"),
    ("meals", "source", "source text not null default 'manual'"),
)

_SCHEMA_STATEMENTS: Sequence[str] = (
    """
    create table if not exists users (
        user_id     integer primary key,
        username    text,
        full_name   text,
        created_at  text not null
    )
    """,
    """
    create table if not exists messages (
        id          integer primary key autoincrement,
        user_id     integer not null,
        chat_id     integer not null,
        text        text not null,
        created_at  text not null,
        day         text not null,
        kind        text not null default 'raw'
    )
    """,
    """
    create table if not exists notes (
        id           integer primary key autoincrement,
        user_id      integer not null,
        message_id   integer,
        text         text not null,
        category     text not null default 'other',
        summary      text,
        created_at   text not null,
        day          text not null
    )
    """,
    """
    create table if not exists meals (
        id           integer primary key autoincrement,
        user_id      integer not null,
        kcal         real not null,
        protein_g    real not null,
        fat_g        real not null,
        carbs_g      real not null,
        created_at   text not null,
        day          text not null,
        description  text,
        source       text not null default 'manual'
    )
    """,
    """
    create table if not exists goals (
        user_id     integer primary key,
        kcal        real not null,
        protein_g   real not null,
        fat_g       real not null,
        carbs_g     real not null,
        updated_at  text not null
    )
    """,
    """
    create table if not exists web_accounts (
        login          text primary key,
        password_hash  text not null,
        role           text not null default 'user',
        tg_user_id     integer,
        created_at     text not null
    )
    """,
    "create index if not exists idx_messages_user_day on messages (user_id, day)",
    "create index if not exists idx_notes_user_day on notes (user_id, day)",
    "create index if not exists idx_meals_user_day on meals (user_id, day)",
)


@dataclass(frozen=True)
class Goal:
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float


@dataclass(frozen=True)
class DayTotals:
    day: str
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    meals_count: int


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_str() -> str:
    return date.today().isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self._db_path: Path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    @property
    def path(self) -> Path:
        return self._db_path

    async def connect(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as error:
            raise RuntimeError(f"нет прав на создание каталога БД: {error}") from error
        except OSError as error:
            raise RuntimeError(f"не удалось создать каталог БД: {error}") from error

        try:
            connection: aiosqlite.Connection = await aiosqlite.connect(self._db_path)
        except sqlite3.OperationalError as error:
            raise RuntimeError(f"не удалось открыть БД {self._db_path}: {error}") from error

        connection.row_factory = aiosqlite.Row
        await connection.execute("pragma journal_mode=wal")
        await connection.execute("pragma foreign_keys=on")
        for statement in _SCHEMA_STATEMENTS:
            await connection.execute(statement)
        await connection.commit()
        self._connection = connection
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        for table, column, definition in _COLUMN_MIGRATIONS:
            if not await self._has_column(table, column):
                connection = self._require_connection()
                await connection.execute(f"alter table {table} add column {definition}")
                await connection.commit()
                logger.info("Миграция БД: %s.%s добавлена", table, column)

    async def _has_column(self, table: str, column: str) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(f"pragma table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        return any(row["name"] == column for row in rows)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._connection

    async def upsert_user(
        self,
        user_id: int,
        username: Optional[str],
        full_name: Optional[str],
    ) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            insert into users (user_id, username, full_name, created_at)
            values (?, ?, ?, ?)
            on conflict(user_id) do update set
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, username, full_name, _now_iso()),
        )
        await connection.commit()

    async def add_message(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        kind: str = "raw",
    ) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            insert into messages (user_id, chat_id, text, created_at, day, kind)
            values (?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, text, _now_iso(), _today_str(), kind),
        )
        await connection.commit()
        return int(cursor.lastrowid or 0)

    async def add_note(
        self,
        user_id: int,
        text: str,
        category: str = "other",
        summary: Optional[str] = None,
        message_id: Optional[int] = None,
    ) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            insert into notes (user_id, message_id, text, category, summary, created_at, day)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, message_id, text, category, summary, _now_iso(), _today_str()),
        )
        await connection.commit()
        return int(cursor.lastrowid or 0)

    async def add_meal(
        self,
        user_id: int,
        kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        description: Optional[str] = None,
        source: str = "manual",
    ) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            insert into meals
                (user_id, kcal, protein_g, fat_g, carbs_g, created_at, day, description, source)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                kcal,
                protein_g,
                fat_g,
                carbs_g,
                _now_iso(),
                _today_str(),
                description,
                source,
            ),
        )
        await connection.commit()
        return int(cursor.lastrowid or 0)

    async def get_day_totals(self, user_id: int, day: Optional[str] = None) -> DayTotals:
        connection = self._require_connection()
        target_day: str = day or _today_str()
        cursor = await connection.execute(
            """
            select
                coalesce(sum(kcal), 0)      as kcal,
                coalesce(sum(protein_g), 0) as protein_g,
                coalesce(sum(fat_g), 0)     as fat_g,
                coalesce(sum(carbs_g), 0)   as carbs_g,
                count(*)                    as meals_count
            from meals
            where user_id = ? and day = ?
            """,
            (user_id, target_day),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return DayTotals(target_day, 0.0, 0.0, 0.0, 0.0, 0)
        return DayTotals(
            day=target_day,
            kcal=float(row["kcal"]),
            protein_g=float(row["protein_g"]),
            fat_g=float(row["fat_g"]),
            carbs_g=float(row["carbs_g"]),
            meals_count=int(row["meals_count"]),
        )

    async def get_day_notes(self, user_id: int, day: str) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            select id, text, category, summary, created_at
            from notes
            where user_id = ? and day = ?
            order by created_at
            """,
            (user_id, day),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_day_messages(self, user_id: int, day: str) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            select id, text, kind, created_at
            from messages
            where user_id = ? and day = ?
            order by created_at
            """,
            (user_id, day),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_day_meals(self, user_id: int, day: str) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            select id, kcal, protein_g, fat_g, carbs_g, created_at, description, source
            from meals
            where user_id = ? and day = ?
            order by created_at
            """,
            (user_id, day),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def list_days(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        user_filter: str = "where user_id = ?" if user_id is not None else ""
        params: List[Any] = [] if user_id is None else [user_id] * 6

        cursor = await connection.execute(
            f"""
            with all_days as (
                select day from meals {user_filter}
                union select day from notes {user_filter}
                union select day from messages {user_filter}
            )
            select
                d.day as day,
                coalesce(m.kcal, 0)      as kcal,
                coalesce(m.protein_g, 0) as protein_g,
                coalesce(m.fat_g, 0)     as fat_g,
                coalesce(m.carbs_g, 0)   as carbs_g,
                coalesce(n.notes_count, 0)    as notes_count,
                coalesce(g.messages_count, 0) as messages_count
            from all_days d
            left join (
                select day,
                       sum(kcal) as kcal,
                       sum(protein_g) as protein_g,
                       sum(fat_g) as fat_g,
                       sum(carbs_g) as carbs_g
                from meals {user_filter} group by day
            ) m on m.day = d.day
            left join (
                select day, count(*) as notes_count from notes {user_filter} group by day
            ) n on n.day = d.day
            left join (
                select day, count(*) as messages_count from messages {user_filter} group by day
            ) g on g.day = d.day
            order by d.day desc
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def set_goal(
        self,
        user_id: int,
        kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
    ) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            insert into goals (user_id, kcal, protein_g, fat_g, carbs_g, updated_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(user_id) do update set
                kcal = excluded.kcal,
                protein_g = excluded.protein_g,
                fat_g = excluded.fat_g,
                carbs_g = excluded.carbs_g,
                updated_at = excluded.updated_at
            """,
            (user_id, kcal, protein_g, fat_g, carbs_g, _now_iso()),
        )
        await connection.commit()

    async def get_goal(self, user_id: Optional[int]) -> Optional[Goal]:
        if user_id is None:
            return None
        connection = self._require_connection()
        cursor = await connection.execute(
            "select kcal, protein_g, fat_g, carbs_g from goals where user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Goal(
            kcal=float(row["kcal"]),
            protein_g=float(row["protein_g"]),
            fat_g=float(row["fat_g"]),
            carbs_g=float(row["carbs_g"]),
        )

    async def delete_goal(self, user_id: int) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute("delete from goals where user_id = ?", (user_id,))
        await connection.commit()
        return cursor.rowcount > 0

    async def create_account(
        self,
        login: str,
        password_hash: str,
        role: str = "user",
        tg_user_id: Optional[int] = None,
    ) -> None:
        connection = self._require_connection()
        try:
            await connection.execute(
                """
                insert into web_accounts (login, password_hash, role, tg_user_id, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (login, password_hash, role, tg_user_id, _now_iso()),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"аккаунт «{login}» уже существует") from error
        await connection.commit()

    async def get_account(self, login: str) -> Optional[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            "select login, password_hash, role, tg_user_id from web_accounts where login = ?",
            (login,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def list_accounts(self) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            "select login, role, tg_user_id, created_at from web_accounts order by login"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def set_account_password(self, login: str, password_hash: str) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            "update web_accounts set password_hash = ? where login = ?",
            (password_hash, login),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def set_account_role(self, login: str, role: str) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute(
            "update web_accounts set role = ? where login = ?", (role, login)
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def delete_account(self, login: str) -> bool:
        connection = self._require_connection()
        cursor = await connection.execute("delete from web_accounts where login = ?", (login,))
        await connection.commit()
        return cursor.rowcount > 0

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        connection = self._require_connection()
        cursor = await connection.execute(
            "select user_id from users where lower(username) = lower(?)",
            (username,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["user_id"]) if row is not None else None

    async def list_users(self) -> List[Dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            "select user_id, username, full_name from users order by user_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]
