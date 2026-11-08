from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User

from bot.formatter import (
    COMPARE_WITH_CALLBACK_PREFIX,
    MENU_CANCEL_TEXT,
    MENU_FIND_CALLBACK,
)
from bot.rate_limit import (
    RATE_LIMITED_MESSAGE,
    RateLimitMiddleware,
    RefreshCooldown,
    SlidingWindowRateLimiter,
)

USER = User(id=100, is_bot=False, first_name="Tester")
CHAT = Chat(id=100, type="private")
URL_ONE = "https://steamcommunity.com/id/test"
URL_TWO = "https://steamcommunity.com/id/rival"


class DummySession(BaseSession):
    """Сессия, которая записывает вызовы Telegram API без сети."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.requests.append(method)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass


@pytest_asyncio.fixture
async def bot() -> AsyncGenerator[Bot, None]:
    bot = Bot(token="42:TEST", session=DummySession())
    yield bot
    await bot.session.close()


def make_message(text: str, *, bot: Bot, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=CHAT,
        from_user=USER,
        text=text,
    ).as_(bot)


def make_callback(
    data: str,
    *,
    bot: Bot,
    callback_id: str = "callback-1",
) -> CallbackQuery:
    return CallbackQuery(
        id=callback_id,
        from_user=USER,
        chat_instance="chat-instance",
        data=data,
        message=make_message("старое сообщение", bot=bot),
    ).as_(bot)


async def run_middleware(
    middleware: RateLimitMiddleware,
    event: Message | CallbackQuery,
    data: dict[str, Any] | None = None,
) -> list[Message | CallbackQuery]:
    """Пропускает событие через middleware и считает вызовы хендлера."""
    calls: list[Message | CallbackQuery] = []

    async def handler(
        current_event: Any,
        _: dict[str, Any],
    ) -> str:
        calls.append(current_event)
        return "handled"

    await middleware(handler, event, data or {})
    return calls


def test_sliding_window_blocks_and_recovers() -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window_seconds=60,
        clock=lambda: now[0],
    )

    for _ in range(5):
        decision = limiter.acquire(1)
        assert decision.allowed is True

    denied = limiter.acquire(1)
    assert denied.allowed is False
    assert denied.notify is True
    assert denied.retry_after == pytest.approx(60.0)

    repeated = limiter.acquire(1)
    assert repeated.allowed is False
    assert repeated.notify is False

    now[0] = 60.1
    assert limiter.acquire(1).allowed is True


def test_sliding_window_purges_stale_users() -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=2,
        window_seconds=10,
        clock=lambda: now[0],
    )

    limiter.acquire(1)
    limiter.acquire(2)
    assert len(limiter) == 2

    now[0] = 11.0
    limiter.purge_stale()
    assert len(limiter) == 0


def test_refresh_cooldown_blocks_and_expires() -> None:
    now = [100.0]
    cooldown = RefreshCooldown(60, clock=lambda: now[0])

    assert cooldown.status(["player-a"]).blocked is False

    cooldown.mark(["player-a"])
    status = cooldown.status(["player-a"])
    assert status.blocked is True
    assert status.elapsed == pytest.approx(0.0)
    assert status.remaining == pytest.approx(60.0)

    now[0] = 130.0
    status = cooldown.status(["player-a"])
    assert status.blocked is True
    assert status.elapsed == pytest.approx(30.0)
    assert status.remaining == pytest.approx(30.0)

    now[0] = 160.0
    assert cooldown.status(["player-a"]).blocked is False

    cooldown.purge_stale()
    assert len(cooldown) == 0


def test_refresh_cooldown_cannot_be_bypassed_by_swapping_players() -> None:
    now = [0.0]
    cooldown = RefreshCooldown(60, clock=lambda: now[0])

    cooldown.mark(["player-a", "player-b"])
    assert cooldown.status(["player-b", "player-a"]).blocked is True


def test_refresh_cooldown_release_allows_retry() -> None:
    now = [100.0]
    cooldown = RefreshCooldown(60, clock=lambda: now[0])

    cooldown.mark(["player-a", "player-b"])
    cooldown.release(["player-a"])

    assert cooldown.status(["player-a"]).blocked is False
    assert cooldown.status(["player-b"]).blocked is True

    cooldown.release(["player-b"])
    assert cooldown.status(["player-b"]).blocked is False
    assert len(cooldown) == 0


def test_refresh_cooldown_cannot_be_bypassed_by_changing_second_player() -> None:
    now = [0.0]
    cooldown = RefreshCooldown(60, clock=lambda: now[0])

    cooldown.mark(["player-a", "player-c"])
    # Меняем второго игрока: общий player-a всё равно должен блокировать.
    assert cooldown.status(["player-a", "player-b"]).blocked is True
    # Пара без общего игрока не блокируется.
    assert cooldown.status(["player-b", "player-d"]).blocked is False

    cooldown.mark(["player-b"])
    assert cooldown.status(["player-a", "player-b"]).blocked is True


def test_refresh_cooldown_auto_purges_stale_entries() -> None:
    now = [0.0]
    cooldown = RefreshCooldown(
        10,
        clock=lambda: now[0],
        cleanup_interval=2,
    )

    cooldown.mark(["player-a"])
    cooldown.mark(["player-b"])
    assert len(cooldown) == 2

    now[0] = 11.0
    cooldown.status(["player-c"])
    cooldown.status(["player-d"])
    assert len(cooldown) == 0


@pytest.mark.asyncio
async def test_middleware_exempts_commands_and_menu(bot: Bot) -> None:
    limiter = SlidingWindowRateLimiter(
        limit=1,
        window_seconds=60,
        clock=lambda: 0.0,
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)

    for index, text in enumerate(
        ["/start", "/help", "/cancel", "/start", "/help"]
    ):
        calls = await run_middleware(
            middleware,
            make_message(text, bot=bot, message_id=index + 1),
        )
        assert len(calls) == 1

    callback = make_callback(MENU_FIND_CALLBACK, bot=bot)
    assert len(await run_middleware(middleware, callback)) == 1
    assert len(limiter) == 0


@pytest.mark.asyncio
async def test_middleware_cancel_texts_do_not_spend_limit(bot: Bot) -> None:
    limiter = SlidingWindowRateLimiter(
        limit=1,
        window_seconds=60,
        clock=lambda: 0.0,
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)

    cancel_texts = [MENU_CANCEL_TEXT, "отмена", "Отмена", "/cancel"]
    for index, text in enumerate(cancel_texts):
        calls = await run_middleware(
            middleware,
            make_message(text, bot=bot, message_id=index + 1),
            {"raw_state": "CompareFlow:waiting_first"},
        )
        assert len(calls) == 1

    # Отмена не израсходовала лимит: обычное действие ещё проходит,
    # а следующее за ним — уже нет.
    assert len(limiter) == 0
    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=10),
        )
    ) == 1
    assert (
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=11),
        )
        == []
    )


@pytest.mark.asyncio
async def test_compare_with_callback_does_not_spend_limit(bot: Bot) -> None:
    limiter = SlidingWindowRateLimiter(
        limit=1,
        window_seconds=60,
        clock=lambda: 0.0,
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)
    callback = make_callback(
        f"{COMPARE_WITH_CALLBACK_PREFIX}76561198000000000",
        bot=bot,
    )

    assert len(await run_middleware(middleware, callback)) == 1
    assert len(limiter) == 0

    # После бесплатного callback обычный поиск ещё проходит, а следующий — нет.
    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=10),
        )
    ) == 1
    assert (
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=11),
        )
        == []
    )


@pytest.mark.asyncio
async def test_middleware_blocks_and_recovers_after_window(bot: Bot) -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=2,
        window_seconds=60,
        clock=lambda: now[0],
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)

    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=1),
        )
    ) == 1
    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=2),
        )
    ) == 1

    assert (
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=3),
        )
        == []
    )
    assert (
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=4),
        )
        == []
    )

    sent = [
        method
        for method in bot.session.requests
        if isinstance(method, SendMessage)
    ]
    assert len(sent) == 1
    assert sent[0].text == RATE_LIMITED_MESSAGE.format(seconds=60)

    now[0] = 61.0
    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=5),
        )
    ) == 1


@pytest.mark.asyncio
async def test_middleware_compare_is_heavier_than_search(bot: Bot) -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=3,
        window_seconds=60,
        clock=lambda: now[0],
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)

    compare_text = f"/compare {URL_ONE} {URL_TWO}"
    assert len(
        await run_middleware(
            middleware,
            make_message(compare_text, bot=bot, message_id=1),
        )
    ) == 1
    assert len(
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=2),
        )
    ) == 1
    assert (
        await run_middleware(
            middleware,
            make_message(URL_ONE, bot=bot, message_id=3),
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_state",
    [
        "CompareFlow:waiting_first",
        "CompareFlow:waiting_second",
    ],
)
async def test_middleware_compare_steps_are_heavy(
    bot: Bot,
    raw_state: str,
) -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=2,
        window_seconds=60,
        clock=lambda: now[0],
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)
    data = {"raw_state": raw_state}

    assert len(
        await run_middleware(
            middleware,
            make_message(URL_TWO, bot=bot, message_id=1),
            data,
        )
    ) == 1
    assert (
        await run_middleware(
            middleware,
            make_message(URL_TWO, bot=bot, message_id=2),
            data,
        )
        == []
    )


@pytest.mark.asyncio
async def test_middleware_notifies_once_and_resets_spinner(bot: Bot) -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        limit=2,
        window_seconds=60,
        clock=lambda: now[0],
    )
    middleware = RateLimitMiddleware(limiter, heavy_weight=2)

    callback_data = f"refresh:{'76561198000000000'}"
    assert len(
        await run_middleware(
            middleware,
            make_callback(callback_data, bot=bot, callback_id="callback-1"),
        )
    ) == 1
    assert (
        await run_middleware(
            middleware,
            make_callback(callback_data, bot=bot, callback_id="callback-2"),
        )
        == []
    )
    assert (
        await run_middleware(
            middleware,
            make_callback(callback_data, bot=bot, callback_id="callback-3"),
        )
        == []
    )

    answers = [
        method
        for method in bot.session.requests
        if isinstance(method, AnswerCallbackQuery)
    ]
    assert len(answers) == 2
    assert answers[0].text == RATE_LIMITED_MESSAGE.format(seconds=60)
    assert answers[0].show_alert is False
    assert answers[1].text is None
