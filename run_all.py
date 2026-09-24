"""Единая точка входа: Telegram-бот и веб-дашборд в одном процессе.

Запуск:  python -m nutrition_bot.run_all

Обе задачи живут в одном asyncio-цикле. Если падает любая из них,
вторая корректно останавливается (Ctrl+C — штатное завершение).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Final, List, Optional, Set

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage

from nutrition_bot.bot import router
from nutrition_bot.config import AppConfig, load_config
from nutrition_bot.database import Database
from nutrition_bot.llm_client import LlmClient
from nutrition_bot.net import create_bot_session

logger: Final[logging.Logger] = logging.getLogger("nutrition_bot.run_all")


async def _run_bot(config: AppConfig, database: Database, llm_client: LlmClient) -> None:
    bot: Bot = Bot(
        token=config.bot_token,
        session=create_bot_session(config.use_threaded_resolver),
    )
    dispatcher: Dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    dispatcher["database"] = database
    dispatcher["llm_client"] = llm_client

    try:
        logger.info("Бот: запускаю polling")
        await dispatcher.start_polling(bot)
    except TelegramAPIError as error:
        logger.error("Бот: ошибка Telegram API: %s", error)
    except asyncio.CancelledError:
        logger.info("Бот: остановка")
        raise
    finally:
        await bot.session.close()


async def _run_web(config: AppConfig) -> None:
    from nutrition_bot.web.app import app as web_app

    server_config: uvicorn.Config = uvicorn.Config(
        app=web_app,
        host=config.web_host,
        port=config.web_port,
        log_level="warning",
    )
    server: uvicorn.Server = uvicorn.Server(server_config)
    # Сигналы обрабатывает главный цикл, а не uvicorn: иначе Ctrl+C
    # перехватывается сервером и бот остаётся висеть.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    logger.info("Дашборд: http://%s:%s", config.web_host, config.web_port)
    serve_task: asyncio.Task = asyncio.create_task(server.serve(), name="uvicorn_serve")
    try:
        await asyncio.shield(serve_task)
    except asyncio.CancelledError:
        logger.info("Дашборд: остановка")
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=10.0)
        except asyncio.TimeoutError:
            serve_task.cancel()
        raise
    except OSError as error:
        logger.error("Дашборд: не удалось занять порт %s: %s", config.web_port, error)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    config: AppConfig = load_config()
    if not config.bot_token:
        logger.error("BOT_TOKEN не задан. Укажи его в nutrition_bot/.env")
        sys.exit(1)

    database: Database = Database(config.db_path)
    await database.connect()
    logger.info("БД готова: %s", database.path)

    llm_client: LlmClient = LlmClient(
        base_url=config.llm_base_url,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
        enabled=config.llm_enabled,
        use_threaded_resolver=config.use_threaded_resolver,
    )
    await llm_client.start()

    bot_task: asyncio.Task = asyncio.create_task(
        _run_bot(config, database, llm_client), name="telegram_bot"
    )
    web_task: asyncio.Task = asyncio.create_task(_run_web(config), name="web_dashboard")
    tasks: List[asyncio.Task] = [bot_task, web_task]

    try:
        done: Set[asyncio.Task]
        pending: Set[asyncio.Task]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            error: Optional[BaseException] = task.exception()
            if error is not None:
                logger.error("Задача %s упала: %s", task.get_name(), error)

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await llm_client.close()
        await database.close()
        logger.info("Остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
