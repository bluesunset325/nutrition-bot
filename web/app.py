
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Final, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from nutrition_bot.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    Account,
    create_session_token,
    is_secure_cookie_env,
    load_or_create_secret_key,
    normalize_login,
    read_session_token,
    verify_password,
)
from nutrition_bot.config import AppConfig, load_config
from nutrition_bot.database import Database, DayTotals, Goal

logger: Final[logging.Logger] = logging.getLogger("nutrition_bot.web")

TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"
templates: Final[Jinja2Templates] = Jinja2Templates(directory=str(TEMPLATES_DIR))

config: Final[AppConfig] = load_config()
database: Final[Database] = Database(config.db_path)

# Задержка при неверном пароле — простейшая защита от перебора.
_FAILED_LOGIN_DELAY_SECONDS: Final[float] = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await database.connect()
    app.state.secret_key = load_or_create_secret_key(config.db_path)
    logger.info("Дашборд подключён к БД: %s", database.path)

    if not await database.list_accounts():
        logger.warning(
            "Аккаунтов дашборда нет — вход невозможен. Создай главный аккаунт: "
            "python -m nutrition_bot.accounts add @твой_username --admin"
        )
    yield
    await database.close()


app: Final[FastAPI] = FastAPI(title="Дневник питания", lifespan=lifespan)


async def _resolve_account(request: Request) -> Optional[Account]:
    token: Optional[str] = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    login: Optional[str] = read_session_token(request.app.state.secret_key, token)
    if login is None:
        return None

    row: Optional[Dict[str, Any]] = await database.get_account(login)
    if row is None:
        return None

    tg_user_id: Optional[int] = row["tg_user_id"]
    if tg_user_id is None:
        # Пользователь мог написать боту уже после создания аккаунта — доклеиваем связь.
        tg_user_id = await database.find_user_id_by_username(login)

    return Account(login=row["login"], role=row["role"], tg_user_id=tg_user_id)


async def require_account(request: Request) -> Account:
    account: Optional[Account] = await _resolve_account(request)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return account


def _effective_user_id(account: Account, requested_user_id: Optional[str]) -> Optional[int]:
    """Чьи данные показывать. Не-админ всегда видит только свои."""
    if not account.is_admin:
        return account.tg_user_id
    if requested_user_id is None or not requested_user_id.strip():
        return None
    try:
        return int(requested_user_id)
    except ValueError:
        return None


@app.exception_handler(HTTPException)
async def redirect_unauthorized(request: Request, exc: HTTPException) -> Response:
    """303 с Location — это наш редирект на /login, остальное отдаём как есть."""
    location: Optional[str] = (exc.headers or {}).get("Location")
    if exc.status_code == status.HTTP_303_SEE_OTHER and location:
        return RedirectResponse(url=location, status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(
        content=f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
        status_code=exc.status_code,
    )


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if await _resolve_account(request) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "users": [], "selected_user_id": None, "account": None},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
) -> Response:
    normalized_login: str = normalize_login(login)
    row: Optional[Dict[str, Any]] = await database.get_account(normalized_login)

    if row is None or not verify_password(password, row["password_hash"]):
        await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
        logger.warning("Неудачный вход: %s", normalized_login)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Неверный логин или пароль.",
                "users": [],
                "selected_user_id": None,
                "account": None,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token: str = create_session_token(request.app.state.secret_key, normalized_login)
    response: RedirectResponse = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie_env(),
    )
    logger.info("Вход: %s (%s)", normalized_login, row["role"])
    return response


@app.post("/logout")
async def logout() -> Response:
    response: RedirectResponse = RedirectResponse(
        url="/login", status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user_id: Optional[str] = None,
    account: Account = Depends(require_account),
) -> HTMLResponse:
    selected_user_id: Optional[int] = _effective_user_id(account, user_id)
    days: List[Dict[str, Any]] = await database.list_days(selected_user_id)
    users: List[Dict[str, Any]] = await database.list_users() if account.is_admin else []
    goal: Optional[Goal] = await database.get_goal(selected_user_id)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "days": days,
            "users": users,
            "goal": goal,
            "selected_user_id": selected_user_id,
            "account": account,
        },
    )


@app.get("/day/{day}", response_class=HTMLResponse)
async def day_detail(
    request: Request,
    day: str,
    user_id: Optional[str] = None,
    account: Account = Depends(require_account),
) -> HTMLResponse:
    selected_user_id: Optional[int] = _effective_user_id(account, user_id)
    users: List[Dict[str, Any]] = await database.list_users() if account.is_admin else []

    if selected_user_id is None:
        if account.is_admin and users:
            selected_user_id = int(users[0]["user_id"])
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Нет привязанных данных: напиши что-нибудь боту в Telegram.",
            )

    totals: DayTotals = await database.get_day_totals(selected_user_id, day)
    meals: List[Dict[str, Any]] = await database.get_day_meals(selected_user_id, day)
    notes: List[Dict[str, Any]] = await database.get_day_notes(selected_user_id, day)
    messages: List[Dict[str, Any]] = await database.get_day_messages(selected_user_id, day)
    goal: Optional[Goal] = await database.get_goal(selected_user_id)

    return templates.TemplateResponse(
        request=request,
        name="day.html",
        context={
            "day": day,
            "totals": totals,
            "goal": goal,
            "meals": meals,
            "notes": notes,
            "messages": messages,
            "users": users,
            "selected_user_id": selected_user_id,
            "account": account,
        },
    )


@app.get("/api/days")
async def api_days(
    user_id: Optional[str] = None,
    account: Account = Depends(require_account),
) -> List[Dict[str, Any]]:
    return await database.list_days(_effective_user_id(account, user_id))


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    run()
