
from __future__ import annotations

import logging
import random
from typing import Final, List, Optional

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

logger: Final[logging.Logger] = logging.getLogger(__name__)

_PHRASES: Final[dict] = {
    "estimate": [
        "Прикидываю калорийность…",
        "Вспоминаю, что обычно кладут в это блюдо…",
        "Считаю КБЖУ порции…",
        "Взвешиваю на глаз…",
        "Оцениваю по типичной подаче…",
    ],
    "note": [
        "Записываю…",
        "Смотрю, о чём это…",
        "Обдумываю…",
    ],
    "summary": [
        "Собираю итоги дня…",
        "Считаю, что вышло за день…",
        "Подвожу черту…",
    ],
}

_FALLBACK_PHRASE: Final[str] = "Думаю…"


def pick_phrase(kind: str) -> str:
    variants: List[str] = _PHRASES.get(kind, [])
    return random.choice(variants) if variants else _FALLBACK_PHRASE


async def send_thinking(message: Message, kind: str) -> Optional[Message]:
    try:
        return await message.answer(pick_phrase(kind))
    except TelegramAPIError as error:
        logger.warning("Не удалось отправить сообщение-размышление: %s", error)
        return None


async def finish_thinking(
    thinking_message: Optional[Message],
    fallback_message: Message,
    text: str,
) -> None:
    if thinking_message is None:
        await fallback_message.answer(text)
        return
    try:
        await thinking_message.edit_text(text)
    except TelegramAPIError as error:
        logger.warning("Не удалось отредактировать сообщение: %s", error)
        await fallback_message.answer(text)
