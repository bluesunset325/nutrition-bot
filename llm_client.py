
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Final, List, Optional, Tuple

import aiohttp

from nutrition_bot.net import create_resolver

logger: Final[logging.Logger] = logging.getLogger(__name__)

_NOTE_CATEGORIES: Final[List[str]] = [
    "training",
    "food",
    "wellbeing",
    "sleep",
    "mood",
    "other",
]

_CLASSIFY_SYSTEM_PROMPT: Final[str] = (
    "Ты — парсер дневника питания и активностей. На вход подаётся одно короткое "
    "сообщение пользователя на русском языке. Верни СТРОГО один JSON-объект без "
    "пояснений и без markdown-обёртки, с полями:\n"
    '  "category": одно из ' + ", ".join(_NOTE_CATEGORIES) + "\n"
    '  "summary": краткая переформулировка сути на русском (до 100 символов)\n'
    "Категория training — упоминание тренировки, зала, пробежки, нагрузки. "
    "food — упоминание еды и срывов. wellbeing — самочувствие, боль, усталость. "
    "sleep — сон. mood — настроение. other — всё остальное."
)

_SUMMARY_SYSTEM_PROMPT: Final[str] = (
    "Ты — тренер-нутрициолог. По данным за день составь короткую сводку на русском "
    "языке: 3-6 строк, без markdown-заголовков, дружелюбно и по делу. Сначала цифры "
    "по калориям и БЖУ, затем краткий вывод по заметкам (тренировки, самочувствие). "
    "Не выдумывай данные, которых нет."
)


# Короткая инструкция плюс примеры-эталоны. Развёрнутые инструкции с
# «ориентирами ккал на 100 г» проверялись и оказались ХУЖЕ: 29% попаданий против
# 58% у этого варианта на одном и том же наборе блюд (qwen2.5:7b, по 3 прогона).
# Модель калибруется по примерам заметно лучше, чем по правилам.
_ESTIMATE_SYSTEM_PROMPT: Final[str] = (
    "Ты — нутрициолог. Оцени КБЖУ порции по описанию. Верни строго JSON: "
    '{"dish":"...","kcal":N,"protein_g":N,"fat_g":N,"carbs_g":N,'
    '"assumption":"...","confidence":"high|medium|low"}. '
    "Считай для всей порции, не на 100 г. Не завышай. "
    "Если вес не указан, прими типичную порцию и укажи её в assumption. "
    "Не добавляй ингредиенты, которых пользователь не упоминал."
)

# Примеры калибруют масштаб оценок. Значения выверены по справочникам.
_ESTIMATE_EXAMPLES: Final[List[Tuple[str, Dict[str, Any]]]] = [
    (
        "плов с бараниной 300 г",
        {
            "dish": "Плов с бараниной", "kcal": 630, "protein_g": 24, "fat_g": 28,
            "carbs_g": 70, "assumption": "порция 300 г", "confidence": "high",
        },
    ),
    (
        "куриная грудка на гриле 200 г",
        {
            "dish": "Куриная грудка на гриле", "kcal": 330, "protein_g": 62, "fat_g": 8,
            "carbs_g": 0, "assumption": "порция 200 г без масла", "confidence": "high",
        },
    ),
    (
        "латте на молоке",
        {
            "dish": "Латте", "kcal": 150, "protein_g": 8, "fat_g": 6, "carbs_g": 15,
            "assumption": "стакан 300 мл, молоко 2.5%", "confidence": "medium",
        },
    ),
]


@dataclass(frozen=True)
class MealEstimate:
    dish: str
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    assumption: str
    confidence: str


@dataclass(frozen=True)
class NoteInsight:
    category: str
    summary: str


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    start: int = raw_text.find("{")
    end: int = raw_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed: Any = json.loads(raw_text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_number(raw_value: Any) -> Optional[float]:
    """Число из ответа модели. Диапазон «300-400» превращается в середину — 350."""
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if not isinstance(raw_value, str):
        return None

    text: str = raw_value.replace(",", ".").strip()
    found: List[str] = re.findall(r"\d+(?:\.\d+)?", text)
    if not found:
        return None

    try:
        numbers: List[float] = [float(item) for item in found]
    except ValueError:
        return None

    # «300-400 ккал» → 350; одиночное число остаётся собой.
    if len(numbers) >= 2 and re.search(r"\d\s*(?:-|–|—|до)\s*\d", text):
        return (numbers[0] + numbers[1]) / 2
    return numbers[0]


_MACRO_MISMATCH_TOLERANCE: Final[float] = 0.10


def _reconcile_macros(numbers: Dict[str, float]) -> Dict[str, float]:
    """Свести БЖУ с калориями.

    Белки и углеводы дают 4 ккал/г, жиры — 9 ккал/г; это физика, а не мнение
    модели. Если сумма по БЖУ расходится с заявленными калориями больше чем на
    10%, масштабируем БЖУ под калории: главное число для пользователя — калории,
    и запись должна быть внутренне непротиворечивой.
    """
    macro_kcal: float = (
        numbers["protein_g"] * 4.0 + numbers["fat_g"] * 9.0 + numbers["carbs_g"] * 4.0
    )
    stated_kcal: float = numbers["kcal"]
    if macro_kcal <= 0 or stated_kcal <= 0:
        return numbers

    deviation: float = abs(macro_kcal - stated_kcal) / stated_kcal
    if deviation <= _MACRO_MISMATCH_TOLERANCE:
        return numbers

    factor: float = stated_kcal / macro_kcal
    logger.info(
        "БЖУ не сходились с калориями (%.0f против %.0f), масштабирую в %.2f раза",
        macro_kcal,
        stated_kcal,
        factor,
    )
    return {
        "kcal": stated_kcal,
        "protein_g": numbers["protein_g"] * factor,
        "fat_g": numbers["fat_g"] * factor,
        "carbs_g": numbers["carbs_g"] * factor,
    }


class LlmClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        enabled: bool = True,
        use_threaded_resolver: bool = True,
    ) -> None:
        self._base_url: str = base_url.rstrip("/")
        self._model: str = model
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._enabled: bool = enabled
        self._use_threaded_resolver: bool = use_threaded_resolver
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if self._session is not None:
            return
        connector: Optional[aiohttp.TCPConnector] = None
        if self._use_threaded_resolver:
            connector = aiohttp.TCPConnector(resolver=create_resolver())
        self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        examples: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
    ) -> Optional[str]:
        """Один вызов /chat/completions. None — если модель недоступна.

        `examples` — пары «запрос пользователя → эталонный ответ», которые
        подмешиваются в диалог перед настоящим вопросом (few-shot).
        """
        if not self._enabled:
            return None
        if self._session is None:
            await self.start()
        session: aiohttp.ClientSession = self._session  # type: ignore[assignment]

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for example_prompt, example_answer in examples or []:
            messages.append({"role": "user", "content": example_prompt})
            messages.append(
                {"role": "assistant", "content": json.dumps(example_answer, ensure_ascii=False)}
            )
        messages.append({"role": "user", "content": user_prompt})

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        try:
            async with session.post(f"{self._base_url}/chat/completions", json=payload) as response:
                if response.status != 200:
                    body: str = await response.text()
                    logger.warning("LLM ответила %s: %s", response.status, body[:200])
                    return None
                data: Dict[str, Any] = await response.json()
        except aiohttp.ClientConnectorError:
            logger.warning("LLM недоступна по адресу %s", self._base_url)
            return None
        except aiohttp.ContentTypeError:
            logger.warning("LLM вернула не-JSON ответ")
            return None
        except aiohttp.ClientError as error:
            logger.warning("Ошибка HTTP при обращении к LLM: %s", error)
            return None
        except asyncio.TimeoutError:
            logger.warning("Таймаут запроса к LLM")
            return None

        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            logger.warning("Неожиданная структура ответа LLM: %r", data)
            return None

    async def analyze_note(self, text: str) -> NoteInsight:
        raw_answer: Optional[str] = await self._chat(
            system_prompt=_CLASSIFY_SYSTEM_PROMPT,
            user_prompt=text,
            temperature=0.0,
        )
        if raw_answer is None:
            return _fallback_note_insight(text)

        parsed: Optional[Dict[str, Any]] = _extract_json_object(raw_answer)
        if parsed is None:
            return _fallback_note_insight(text)

        category: str = str(parsed.get("category", "other")).strip().lower()
        if category not in _NOTE_CATEGORIES:
            category = "other"
        summary: str = str(parsed.get("summary", "")).strip() or text[:100]
        return NoteInsight(category=category, summary=summary[:200])

    async def estimate_meal(self, description: str) -> Optional[MealEstimate]:
        raw_answer: Optional[str] = await self._chat(
            system_prompt=_ESTIMATE_SYSTEM_PROMPT,
            user_prompt=description,
            temperature=0.2,
            examples=_ESTIMATE_EXAMPLES,
        )
        if raw_answer is None:
            return None

        parsed: Optional[Dict[str, Any]] = _extract_json_object(raw_answer)
        if parsed is None:
            logger.warning("Оценка блюда: не удалось разобрать JSON из ответа модели")
            return None

        numbers: Dict[str, float] = {}
        for field in ("kcal", "protein_g", "fat_g", "carbs_g"):
            value: Optional[float] = _coerce_number(parsed.get(field))
            if value is None:
                logger.warning("Оценка блюда: поле %s не является числом", field)
                return None
            numbers[field] = value

        if numbers["kcal"] <= 0 or numbers["kcal"] > 20_000:
            logger.warning("Оценка блюда: неправдоподобные калории %s", numbers["kcal"])
            return None

        numbers = _reconcile_macros(numbers)

        confidence: str = str(parsed.get("confidence", "medium")).strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"

        return MealEstimate(
            dish=str(parsed.get("dish", "")).strip() or description[:80],
            kcal=numbers["kcal"],
            protein_g=numbers["protein_g"],
            fat_g=numbers["fat_g"],
            carbs_g=numbers["carbs_g"],
            assumption=str(parsed.get("assumption", "")).strip(),
            confidence=confidence,
        )

    async def build_day_summary(self, summary_payload: str) -> Optional[str]:
        return await self._chat(
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            user_prompt=summary_payload,
            temperature=0.3,
        )


_TRAINING_KEYWORDS: Final[List[str]] = [
    "зал", "трениров", "пробеж", "бег", "штанг", "присед", "жим",
    "кардио", "качал", "workout", "gym", "подход",
]
_FOOD_KEYWORDS: Final[List[str]] = ["ел", "съел", "пицц", "срыв", "перекус", "обед", "ужин", "завтрак"]
_SLEEP_KEYWORDS: Final[List[str]] = ["спал", "сон", "выспал", "бессонн"]
_WELLBEING_KEYWORDS: Final[List[str]] = ["устал", "болит", "самочувств", "разбит", "энерг"]
_MOOD_KEYWORDS: Final[List[str]] = ["настроен", "злюсь", "радост", "стресс", "тревог"]


def _fallback_note_insight(text: str) -> NoteInsight:
    lowered: str = text.lower()
    groups: List[tuple] = [
        ("training", _TRAINING_KEYWORDS),
        ("sleep", _SLEEP_KEYWORDS),
        ("wellbeing", _WELLBEING_KEYWORDS),
        ("mood", _MOOD_KEYWORDS),
        ("food", _FOOD_KEYWORDS),
    ]
    for category, keywords in groups:
        if any(keyword in lowered for keyword in keywords):
            return NoteInsight(category=category, summary=text[:100])
    return NoteInsight(category="other", summary=text[:100])
