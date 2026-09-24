"""Telegram-бот учёта питания и активностей (aiogram 3.x).

Запуск:  python -m nutrition_bot.bot
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Final, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from nutrition_bot.config import AppConfig, load_config
from nutrition_bot.database import Database, DayTotals, Goal
from nutrition_bot.llm_client import LlmClient, MealEstimate, NoteInsight
from nutrition_bot.net import create_bot_session
from nutrition_bot.thinking import finish_thinking, send_thinking

logger: Final[logging.Logger] = logging.getLogger("nutrition_bot")

router: Final[Router] = Router(name="main")

_CALORIE_TRIGGER_PATTERN: Final[re.Pattern] = re.compile(
    r"(калори|ккал|kcal|ккл|еда|поел|покушал|приём\s*пищи|прием\s*пищи|бжу)",
    re.IGNORECASE,
)

_CANCEL_WORDS: Final[List[str]] = ["отмена", "стоп", "cancel", "/cancel"]

_CATEGORY_TITLES: Final[dict] = {
    "training": "🏋️ тренировка",
    "food": "🍽 еда",
    "wellbeing": "💤 самочувствие",
    "sleep": "😴 сон",
    "mood": "🙂 настроение",
    "other": "📝 заметка",
}


class GoalStates(StatesGroup):
    waiting_for_kcal = State()
    waiting_for_protein = State()
    waiting_for_fat = State()
    waiting_for_carbs = State()


class MealStates(StatesGroup):
    waiting_for_food = State()
    waiting_for_kcal = State()
    waiting_for_protein = State()
    waiting_for_fat = State()
    waiting_for_carbs = State()


def parse_positive_number(raw_text: str) -> Optional[float]:
    """Разобрать число из свободного ввода ('530', '12,5 г', '~40')."""
    normalized: str = raw_text.replace(",", ".").strip()
    match: Optional[re.Match] = re.search(r"\d+(?:\.\d+)?", normalized)
    if match is None:
        return None
    try:
        value: float = float(match.group(0))
    except ValueError:
        return None
    if value < 0 or value > 100_000:
        return None
    return value


def progress_bar(consumed: float, target: float, width: int = 10) -> str:
    """Полоска прогресса: ████░░░░░░ 40%. Перебор отмечается отдельно."""
    if target <= 0:
        return ""
    ratio: float = consumed / target
    filled: int = min(width, max(0, round(ratio * width)))
    bar: str = "█" * filled + "░" * (width - filled)
    return f"{bar} {ratio * 100:.0f}%"


def _format_line(title: str, consumed: float, target: Optional[float], unit: str) -> str:
    precision: int = 0 if unit == "ккал" else 1
    consumed_text: str = f"{consumed:.{precision}f}"
    if target is None or target <= 0:
        return f"• {title}: {consumed_text} {unit}"

    remaining: float = target - consumed
    if remaining >= 0:
        tail: str = f"осталось {remaining:.{precision}f}"
    else:
        tail = f"перебор на {abs(remaining):.{precision}f}"
    return (
        f"• {title}: {consumed_text} / {target:.{precision}f} {unit}  "
        f"{progress_bar(consumed, target)}  ({tail})"
    )


def format_totals(totals: DayTotals, goal: Optional[Goal] = None) -> str:
    header: str = f"📊 Итого за {totals.day} (приёмов пищи: {totals.meals_count}):"
    lines: List[str] = [
        _format_line("Калории", totals.kcal, goal.kcal if goal else None, "ккал"),
        _format_line("Белки", totals.protein_g, goal.protein_g if goal else None, "г"),
        _format_line("Жиры", totals.fat_g, goal.fat_g if goal else None, "г"),
        _format_line("Углеводы", totals.carbs_g, goal.carbs_g if goal else None, "г"),
    ]
    if goal is None:
        lines.append("\nЦель на день не задана — установить: /goal")
    return header + "\n" + "\n".join(lines)


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in _CANCEL_WORDS


@router.message(Command("start"))
async def handle_start(message: Message, database: Database) -> None:
    if message.from_user is None:
        return
    await database.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await message.answer(
        "Привет! Я веду твой дневник питания и активностей.\n\n"
        "• Просто напиши любую мысль («устал в зале», «сорвался на пиццу») — "
        "я сохраню её как заметку дня.\n"
        "• Напиши «калории» — задам 4 вопроса (ккал, белки, жиры, углеводы) "
        "и покажу итог за день.\n"
        "• /goal — задать дневную цель по КБЖУ (или сразу: /goal 2000 150 60 200), "
        "после этого итоги показываются с прогрессом и остатком.\n"
        "• /today — сводка за сегодня, /cancel — прервать опрос."
    )


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    current_state: Optional[str] = await state.get_state()
    await state.clear()
    if current_state is None:
        await message.answer("Сейчас нечего отменять.")
    else:
        await message.answer("Опрос отменён. Данные не сохранены.")


@router.message(Command("goal"))
async def handle_goal(
    message: Message,
    state: FSMContext,
    database: Database,
    command: CommandObject,
) -> None:
    """/goal — пошагово; /goal 2000 150 60 200 — сразу; /goal сброс — удалить."""
    if message.from_user is None:
        return
    user_id: int = message.from_user.id
    arguments: str = (command.args or "").strip()

    if arguments.lower() in {"сброс", "удалить", "off", "reset"}:
        if await database.delete_goal(user_id):
            await message.answer("Цель удалена. Итоги будут показываться без прогресса.")
        else:
            await message.answer("Цель и так не задана.")
        return

    if arguments:
        parts: List[str] = arguments.split()
        if len(parts) != 4:
            await message.answer(
                "Нужно четыре числа: ккал, белки, жиры, углеводы.\n"
                "Например: /goal 2000 150 60 200"
            )
            return
        values: List[Optional[float]] = [parse_positive_number(part) for part in parts]
        if any(value is None for value in values):
            await message.answer("Не все значения — числа. Например: /goal 2000 150 60 200")
            return
        await database.set_goal(
            user_id=user_id,
            kcal=float(values[0] or 0.0),
            protein_g=float(values[1] or 0.0),
            fat_g=float(values[2] or 0.0),
            carbs_g=float(values[3] or 0.0),
        )
        goal: Optional[Goal] = await database.get_goal(user_id)
        totals: DayTotals = await database.get_day_totals(user_id)
        await message.answer("🎯 Цель сохранена.\n\n" + format_totals(totals, goal))
        return

    current_goal: Optional[Goal] = await database.get_goal(user_id)
    current_text: str = (
        f"Сейчас: {current_goal.kcal:.0f} ккал / Б {current_goal.protein_g:.0f}"
        f" / Ж {current_goal.fat_g:.0f} / У {current_goal.carbs_g:.0f}\n\n"
        if current_goal is not None
        else ""
    )
    await state.set_state(GoalStates.waiting_for_kcal)
    await message.answer(
        f"{current_text}Цель по калориям на день? (число)\n"
        "Можно и одной строкой: /goal 2000 150 60 200\nПрервать — /cancel"
    )


@router.message(GoalStates.waiting_for_kcal, F.text)
async def handle_goal_kcal(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Установка цели отменена.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Например: 2000")
        return
    await state.update_data(goal_kcal=value)
    await state.set_state(GoalStates.waiting_for_protein)
    await message.answer("Цель по белкам, г?")


@router.message(GoalStates.waiting_for_protein, F.text)
async def handle_goal_protein(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Установка цели отменена.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Например: 150")
        return
    await state.update_data(goal_protein=value)
    await state.set_state(GoalStates.waiting_for_fat)
    await message.answer("Цель по жирам, г?")


@router.message(GoalStates.waiting_for_fat, F.text)
async def handle_goal_fat(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Установка цели отменена.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Например: 60")
        return
    await state.update_data(goal_fat=value)
    await state.set_state(GoalStates.waiting_for_carbs)
    await message.answer("Цель по углеводам, г?")


@router.message(GoalStates.waiting_for_carbs, F.text)
async def handle_goal_carbs(message: Message, state: FSMContext, database: Database) -> None:
    if message.text is None or message.from_user is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Установка цели отменена.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Например: 200")
        return

    data: dict = await state.get_data()
    await state.clear()
    user_id: int = message.from_user.id
    await database.set_goal(
        user_id=user_id,
        kcal=float(data.get("goal_kcal", 0.0)),
        protein_g=float(data.get("goal_protein", 0.0)),
        fat_g=float(data.get("goal_fat", 0.0)),
        carbs_g=value,
    )
    goal: Optional[Goal] = await database.get_goal(user_id)
    totals: DayTotals = await database.get_day_totals(user_id)
    await message.answer("🎯 Цель сохранена.\n\n" + format_totals(totals, goal))


@router.message(Command("today"))
async def handle_today(
    message: Message,
    database: Database,
    llm_client: LlmClient,
) -> None:
    if message.from_user is None:
        return
    user_id: int = message.from_user.id
    totals: DayTotals = await database.get_day_totals(user_id)
    notes: List[dict] = await database.get_day_notes(user_id, totals.day)

    goal: Optional[Goal] = await database.get_goal(user_id)
    await message.answer(format_totals(totals, goal))

    thinking_message: Optional[Message] = None
    if llm_client.enabled:
        thinking_message = await send_thinking(message, "summary")

    llm_summary: Optional[str] = await _build_llm_summary(llm_client, totals, notes)
    if llm_summary is not None:
        await finish_thinking(thinking_message, message, f"🤖 Сводка дня:\n{llm_summary}")
        return

    if thinking_message is not None:
        await finish_thinking(thinking_message, message, "Сводка недоступна: модель не отвечает.")

    if notes:
        lines: List[str] = [
            f"{_CATEGORY_TITLES.get(str(note['category']), '📝 заметка')}: "
            f"{note['summary'] or note['text']}"
            for note in notes
        ]
        await message.answer("Заметки дня:\n" + "\n".join(lines))


async def _build_llm_summary(
    llm_client: LlmClient,
    totals: DayTotals,
    notes: List[dict],
) -> Optional[str]:
    notes_block: str = "\n".join(
        f"- [{note['category']}] {note['summary'] or note['text']}" for note in notes
    ) or "- заметок нет"
    payload: str = (
        f"Дата: {totals.day}\n"
        f"Калории: {totals.kcal:.0f} ккал\n"
        f"Белки: {totals.protein_g:.1f} г\n"
        f"Жиры: {totals.fat_g:.1f} г\n"
        f"Углеводы: {totals.carbs_g:.1f} г\n"
        f"Приёмов пищи: {totals.meals_count}\n"
        f"Заметки:\n{notes_block}"
    )
    return await llm_client.build_day_summary(payload)


@router.message(StateFilter(None), F.text.regexp(_CALORIE_TRIGGER_PATTERN))
async def handle_start_meal_survey(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    if message.from_user is None or message.text is None:
        return
    await database.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await database.add_message(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        text=message.text,
        kind="meal_trigger",
    )
    await state.set_state(MealStates.waiting_for_food)
    await message.answer(
        "Что ел? Опиши блюдо и вес — прикину КБЖУ сам.\n"
        "Например: «паста карбонара 350 г» или «2 сырника со сметаной».\n\n"
        "Знаешь точные цифры — пришли их в строку: «530 32 14 60» "
        "(ккал, Б, Ж, У) или одно число, чтобы вводить по шагам.\n"
        "Прервать — /cancel"
    )


def parse_number_row(raw_text: str) -> Optional[List[float]]:
    """«530 32 14 60» → [530, 32, 14, 60]. None — если это не строка из чисел."""
    parts: List[str] = raw_text.replace(",", ".").split()
    if not parts:
        return None
    values: List[float] = []
    for part in parts:
        if not re.fullmatch(r"\d+(?:\.\d+)?", part):
            return None
        values.append(float(part))
    return values


def format_estimate(estimate: MealEstimate, totals: DayTotals, goal: Optional[Goal]) -> str:
    confidence_titles: dict = {
        "high": "уверенно",
        "medium": "примерно",
        "low": "очень грубо",
    }
    confidence_text: str = confidence_titles.get(estimate.confidence, "примерно")
    lines: List[str] = [
        f"🍽 {estimate.dish} — оценка {confidence_text}:",
        f"• {estimate.kcal:.0f} ккал",
        f"• Б {estimate.protein_g:.0f} г · Ж {estimate.fat_g:.0f} г · У {estimate.carbs_g:.0f} г",
    ]
    if estimate.assumption:
        lines.append(f"_{estimate.assumption}_".replace("_", ""))
    lines.append("")
    lines.append(format_totals(totals, goal))
    lines.append("")
    lines.append("Цифры прикидочные. Если знаешь точные — пришли «калории» и введи их числами.")
    return "\n".join(lines)


async def _save_meal_and_reply(
    message: Message,
    database: Database,
    user_id: int,
    kcal: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    description: Optional[str],
    source: str,
) -> tuple:
    await database.add_meal(
        user_id=user_id,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        description=description,
        source=source,
    )
    totals: DayTotals = await database.get_day_totals(user_id)
    goal: Optional[Goal] = await database.get_goal(user_id)
    return totals, goal


@router.message(MealStates.waiting_for_food, F.text)
async def handle_food_description(
    message: Message,
    state: FSMContext,
    database: Database,
    llm_client: LlmClient,
) -> None:
    if message.text is None or message.from_user is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Опрос отменён.")
        return

    user_id: int = message.from_user.id
    numbers: Optional[List[float]] = parse_number_row(message.text)

    # Четыре числа подряд — записываем сразу, без вопросов.
    if numbers is not None and len(numbers) == 4:
        await state.clear()
        totals, goal = await _save_meal_and_reply(
            message, database, user_id,
            kcal=numbers[0], protein_g=numbers[1], fat_g=numbers[2], carbs_g=numbers[3],
            description=None, source="manual",
        )
        await message.answer("✅ Записал приём пищи.\n\n" + format_totals(totals, goal))
        return

    # Одно число — это калории, дальше идём по шагам, как раньше.
    if numbers is not None and len(numbers) == 1:
        await state.update_data(kcal=numbers[0])
        await state.set_state(MealStates.waiting_for_protein)
        await message.answer("Сколько белков, г?")
        return

    # Всё остальное — описание блюда: считает модель.
    thinking_message: Optional[Message] = await send_thinking(message, "estimate")
    estimate: Optional[MealEstimate] = await llm_client.estimate_meal(message.text)

    if estimate is None:
        await state.set_state(MealStates.waiting_for_kcal)
        await finish_thinking(
            thinking_message,
            message,
            "Не смог оценить блюдо: локальная модель не отвечает "
            "(запусти Ollama) или вернула ерунду.\n\n"
            "Давай по цифрам — сколько килокалорий?",
        )
        return

    await state.clear()
    totals, goal = await _save_meal_and_reply(
        message, database, user_id,
        kcal=estimate.kcal, protein_g=estimate.protein_g,
        fat_g=estimate.fat_g, carbs_g=estimate.carbs_g,
        description=estimate.dish, source="llm",
    )
    await finish_thinking(thinking_message, message, format_estimate(estimate, totals, goal))


@router.message(MealStates.waiting_for_kcal, F.text)
async def handle_kcal(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Опрос отменён.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Напиши, например: 530")
        return
    await state.update_data(kcal=value)
    await state.set_state(MealStates.waiting_for_protein)
    await message.answer("Сколько белков, г?")


@router.message(MealStates.waiting_for_protein, F.text)
async def handle_protein(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Опрос отменён.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Напиши, например: 32")
        return
    await state.update_data(protein_g=value)
    await state.set_state(MealStates.waiting_for_fat)
    await message.answer("Сколько жиров, г?")


@router.message(MealStates.waiting_for_fat, F.text)
async def handle_fat(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Опрос отменён.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Напиши, например: 14")
        return
    await state.update_data(fat_g=value)
    await state.set_state(MealStates.waiting_for_carbs)
    await message.answer("Сколько углеводов, г?")


@router.message(MealStates.waiting_for_carbs, F.text)
async def handle_carbs(
    message: Message,
    state: FSMContext,
    database: Database,
    llm_client: LlmClient,
) -> None:
    if message.text is None or message.from_user is None:
        return
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("Опрос отменён.")
        return
    value: Optional[float] = parse_positive_number(message.text)
    if value is None:
        await message.answer("Не понял число. Напиши, например: 60")
        return

    data: dict = await state.get_data()
    await state.clear()

    user_id: int = message.from_user.id
    await database.add_meal(
        user_id=user_id,
        kcal=float(data.get("kcal", 0.0)),
        protein_g=float(data.get("protein_g", 0.0)),
        fat_g=float(data.get("fat_g", 0.0)),
        carbs_g=value,
    )

    totals: DayTotals = await database.get_day_totals(user_id)
    goal: Optional[Goal] = await database.get_goal(user_id)
    await message.answer("✅ Записал приём пищи.\n\n" + format_totals(totals, goal))

    notes: List[dict] = await database.get_day_notes(user_id, totals.day)
    llm_summary: Optional[str] = await _build_llm_summary(llm_client, totals, notes)
    if llm_summary is not None:
        await message.answer(f"🤖 Сводка дня:\n{llm_summary}")


@router.message(StateFilter(None), F.text)
async def handle_free_note(
    message: Message,
    database: Database,
    llm_client: LlmClient,
) -> None:
    if message.text is None or message.from_user is None:
        return
    user_id: int = message.from_user.id
    await database.upsert_user(
        user_id=user_id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    stored_message_id: int = await database.add_message(
        user_id=user_id,
        chat_id=message.chat.id,
        text=message.text,
        kind="note",
    )

    thinking_message: Optional[Message] = None
    if llm_client.enabled:
        thinking_message = await send_thinking(message, "note")

    insight: NoteInsight = await llm_client.analyze_note(message.text)
    await database.add_note(
        user_id=user_id,
        text=message.text,
        category=insight.category,
        summary=insight.summary,
        message_id=stored_message_id,
    )

    title: str = _CATEGORY_TITLES.get(insight.category, "📝 заметка")
    await finish_thinking(thinking_message, message, f"Записал ({title}).")


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

    bot: Bot = Bot(token=config.bot_token, session=create_bot_session(config.use_threaded_resolver))
    dispatcher: Dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    dispatcher["database"] = database
    dispatcher["llm_client"] = llm_client

    try:
        await dispatcher.start_polling(bot)
    except TelegramAPIError as error:
        logger.error("Ошибка Telegram API: %s", error)
    except asyncio.CancelledError:
        logger.info("Остановка по сигналу отмены")
    finally:
        await bot.session.close()
        await llm_client.close()
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
