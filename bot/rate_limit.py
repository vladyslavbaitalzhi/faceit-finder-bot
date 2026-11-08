from __future__ import annotations

import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.formatter import (
    CANCEL_TEXTS,
    COMPARE_WITH_CALLBACK_PREFIX,
    MENU_CANCEL_CALLBACK,
    MENU_CANCEL_TEXT,
    MENU_COMPARE_CALLBACK,
    MENU_COMPARE_TEXT,
    MENU_FIND_CALLBACK,
    MENU_FIND_TEXT,
    MENU_HELP_CALLBACK,
    MENU_HELP_TEXT,
    MENU_MAIN_CALLBACK,
)

logger = logging.getLogger(__name__)

RATE_LIMITED_MESSAGE = "Слишком много запросов, подожди {seconds} сек."
COMPARE_FIRST_STATE = "CompareFlow:waiting_first"
COMPARE_SECOND_STATE = "CompareFlow:waiting_second"

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
EXEMPT_COMMANDS = {"/start", "/help", "/cancel"}
EXEMPT_MENU_TEXTS = {
    MENU_FIND_TEXT,
    MENU_COMPARE_TEXT,
    MENU_HELP_TEXT,
    MENU_CANCEL_TEXT,
}
EXEMPT_MENU_CALLBACKS = {
    MENU_FIND_CALLBACK,
    MENU_COMPARE_CALLBACK,
    MENU_HELP_CALLBACK,
    MENU_CANCEL_CALLBACK,
    MENU_MAIN_CALLBACK,
}
REFRESH_CALLBACK_PREFIXES = ("refresh:", "refresh_compare:")
EXEMPT_CALLBACK_PREFIXES = (COMPARE_WITH_CALLBACK_PREFIX,)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Решение лимитера по одному действию пользователя."""

    allowed: bool
    retry_after: float = 0.0
    notify: bool = False


class SlidingWindowRateLimiter:
    """Взвешенный лимитер со скользящим окном в памяти процесса."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        cleanup_interval: int = 100,
    ) -> None:
        if limit < 1:
            raise ValueError("limit должен быть больше 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds должен быть больше 0")
        if cleanup_interval < 1:
            raise ValueError("cleanup_interval должен быть больше 0")

        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._events: dict[int, deque[tuple[float, int]]] = {}
        self._notified_until: dict[int, float] = {}
        self._operations = 0

    def acquire(self, user_id: int, cost: int = 1) -> RateLimitDecision:
        """Проверяет и учитывает действие пользователя."""
        cost = min(max(1, int(cost)), self._limit)
        now = self._clock()

        self._operations += 1
        if self._operations % self._cleanup_interval == 0:
            self.purge_stale(now)

        events = self._events.setdefault(user_id, deque())
        cutoff = now - self._window
        while events and events[0][0] <= cutoff:
            events.popleft()

        total = sum(weight for _, weight in events)
        if total + cost <= self._limit:
            events.append((now, cost))
            return RateLimitDecision(allowed=True)

        needed = total + cost - self._limit
        retry_at = now + self._window
        accumulated = 0
        for timestamp, weight in events:
            accumulated += weight
            if accumulated >= needed:
                retry_at = timestamp + self._window
                break

        retry_after = max(0.0, retry_at - now)
        notified_until = self._notified_until.get(user_id, 0.0)
        notify = now >= notified_until
        if notify:
            self._notified_until[user_id] = retry_at

        return RateLimitDecision(
            allowed=False,
            retry_after=retry_after,
            notify=notify,
        )

    def purge_stale(self, now: float | None = None) -> None:
        """Удаляет записи пользователей, чьи окна уже истекли."""
        current = self._clock() if now is None else now
        cutoff = current - self._window

        for user_id, events in list(self._events.items()):
            while events and events[0][0] <= cutoff:
                events.popleft()
            if not events and self._notified_until.get(user_id, 0.0) <= current:
                self._events.pop(user_id, None)
                self._notified_until.pop(user_id, None)

        for user_id, until in list(self._notified_until.items()):
            if user_id not in self._events and until <= current:
                self._notified_until.pop(user_id, None)

    def __len__(self) -> int:
        """Возвращает число пользователей с активными записями."""
        return len(self._events)


@dataclass(frozen=True, slots=True)
class CooldownStatus:
    """Состояние cooldown для набора целей."""

    blocked: bool
    remaining: float = 0.0
    elapsed: float = 0.0


class RefreshCooldown:
    """Не даёт обновлять одни и те же данные чаще заданного интервала."""

    def __init__(
        self,
        cooldown_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        cleanup_interval: int = 100,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds не может быть отрицательным")
        if cleanup_interval < 1:
            raise ValueError("cleanup_interval должен быть больше 0")

        self._cooldown = cooldown_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._last_refresh: dict[str, float] = {}
        self._operations = 0

    def status(self, targets: Iterable[str]) -> CooldownStatus:
        """Проверяет, обновлялась ли недавно хотя бы одна из целей.

        Проверка по каждой цели отдельно не позволяет обойти cooldown
        перестановкой игроков A/B или заменой второго игрока в сравнении.
        """
        self._maybe_purge()

        unique_targets = list(dict.fromkeys(targets))
        if not unique_targets:
            return CooldownStatus(blocked=False)

        now = self._clock()
        blocked_timestamps: list[float] = []
        all_timestamps: list[float] = []
        for target in unique_targets:
            timestamp = self._last_refresh.get(target)
            if timestamp is None:
                continue
            all_timestamps.append(timestamp)
            if now - timestamp < self._cooldown:
                blocked_timestamps.append(timestamp)

        if blocked_timestamps:
            # Показываем, насколько свежа самая недавняя из заблокированных
            # целей: именно она определяет оставшееся ожидание.
            elapsed = max(
                0.0,
                min(now - timestamp for timestamp in blocked_timestamps),
            )
            return CooldownStatus(
                blocked=True,
                remaining=self._cooldown - elapsed,
                elapsed=elapsed,
            )

        if not all_timestamps:
            return CooldownStatus(blocked=False)

        elapsed = max(0.0, min(now - timestamp for timestamp in all_timestamps))
        return CooldownStatus(blocked=False, elapsed=elapsed)

    def mark(self, targets: Iterable[str]) -> None:
        """Запоминает успешное обновление целей."""
        self._maybe_purge()
        now = self._clock()
        for target in dict.fromkeys(targets):
            self._last_refresh[target] = now

    def release(self, targets: Iterable[str]) -> None:
        """Снимает предварительную отметку refresh после ошибки загрузки."""
        for target in dict.fromkeys(targets):
            self._last_refresh.pop(target, None)

    def _maybe_purge(self) -> None:
        """Периодически очищает устаревшие цели, чтобы словарь не рос."""
        self._operations += 1
        if self._operations % self._cleanup_interval == 0:
            self.purge_stale()

    def purge_stale(self) -> None:
        """Удаляет цели, для которых cooldown уже истёк."""
        now = self._clock()
        for target, timestamp in list(self._last_refresh.items()):
            if now - timestamp >= self._cooldown:
                self._last_refresh.pop(target, None)

    def reset(self) -> None:
        """Полностью очищает историю обновлений."""
        self._last_refresh.clear()

    def __len__(self) -> int:
        """Возвращает число целей с активным cooldown."""
        return len(self._last_refresh)


class RateLimitMiddleware(BaseMiddleware):
    """Ограничивает действия пользователей, которые могут тратить квоту Faceit."""

    def __init__(
        self,
        limiter: SlidingWindowRateLimiter,
        *,
        heavy_weight: int = 2,
    ) -> None:
        self._limiter = limiter
        self._heavy_weight = max(2, int(heavy_weight))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        cost = self._event_cost(event, data)
        if cost <= 0:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)
        if user_id is None:
            return await handler(event, data)

        decision = self._limiter.acquire(user_id, cost)
        if decision.allowed:
            return await handler(event, data)

        seconds = max(1, math.ceil(decision.retry_after))
        message = RATE_LIMITED_MESSAGE.format(seconds=seconds)

        if isinstance(event, CallbackQuery):
            try:
                if decision.notify:
                    await event.answer(message, show_alert=False)
                else:
                    # Сбрасываем спиннер, но больше не засоряем чат.
                    await event.answer()
            except TelegramBadRequest:
                logger.debug("Не удалось ответить на callback при rate limit")
        elif isinstance(event, Message) and decision.notify:
            try:
                await event.answer(message)
            except TelegramBadRequest:
                logger.debug("Не удалось ответить на сообщение при rate limit")

        return None

    def _event_cost(
        self,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> int:
        """Возвращает стоимость события; 0 — событие не лимитируется."""
        if isinstance(event, CallbackQuery):
            return self._callback_cost(event.data or "")
        if isinstance(event, Message):
            raw_state = data.get("raw_state")
            return self._message_cost(event.text or "", raw_state)
        return 0

    def _callback_cost(self, callback_data: str) -> int:
        if callback_data in EXEMPT_MENU_CALLBACKS:
            return 0
        # Быстрый старт сравнения из профиля не делает сетевых запросов.
        if callback_data.startswith(EXEMPT_CALLBACK_PREFIXES):
            return 0
        if callback_data.startswith(REFRESH_CALLBACK_PREFIXES):
            return self._heavy_weight
        return 0

    def _message_cost(self, text: str, raw_state: object) -> int:
        stripped = text.strip()
        if not stripped:
            return 0
        if stripped.casefold() in CANCEL_TEXTS:
            return 0
        if stripped in EXEMPT_MENU_TEXTS:
            return 0

        parts = stripped.split(maxsplit=1)
        command = parts[0].split("@", maxsplit=1)[0].casefold()
        if command in EXEMPT_COMMANDS:
            return 0
        if command == "/compare":
            return self._heavy_weight if len(parts) > 1 else 0

        # Любой ввод внутри FSM-сравнения запускает загрузку двух игроков.
        if raw_state in (COMPARE_FIRST_STATE, COMPARE_SECOND_STATE):
            return self._heavy_weight

        urls = URL_RE.findall(stripped)
        if len(urls) >= 2:
            return self._heavy_weight
        if len(urls) == 1:
            return 1
        return 0
