from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    EditMessageText,
    AnswerCallbackQuery,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import handlers
from bot.steam import SteamStats
from bot.stats import RECENT_MATCHES_FETCH_LIMIT

STEAM_ID64 = "76561198000000000"
PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"
CHAT = Chat(id=100, type="private")
USER = User(id=100, is_bot=False, first_name="Tester")

PLAYER = {
    "player_id": PLAYER_ID,
    "nickname": "TestPlayer",
    "country": "kz",
    "faceit_url": "https://www.faceit.com/ru/players/TestPlayer",
    "games": {
        "cs2": {
            "region": "EU",
            "faceit_elo": 2345,
            "skill_level": 10,
            "max_faceit_elo": 2600,
        }
    },
}

LIFETIME = {
    "player_id": PLAYER_ID,
    "lifetime": {
        "Matches": "120",
        "Win Rate %": "55",
        "Average K/D Ratio": "1.25",
        "Average Headshots %": "48",
        "Longest Win Streak": "10",
        "Current Win Streak": "1",
    },
    "segments": [],
}

RECENT = {
    "player_id": PLAYER_ID,
    "items": [
        {
            "match_id": "match-1",
            "elo": 2400,
            "stats": {
                "Result": "1",
                "K/D Ratio": "2.0",
                "Kills": "20",
                "Deaths": "10",
                "Assists": "5",
                "Map": "de_mirage",
            },
        },
        {
            "match_id": "match-2",
            "elo": 2380,
            "stats": {
                "Result": "0",
                "K/D Ratio": "0.5",
                "Kills": "10",
                "Deaths": "20",
                "Assists": "2",
                "Map": "de_dust2",
            },
        },
    ],
}

MATCH_DETAILS = {
    "match-1": {
        "results": {
            "score": {"faction1": 13, "faction2": 9},
            "winner": "faction1",
        },
        "teams": {
            "faction1": {"roster": [{"player_id": PLAYER_ID}]},
            "faction2": {"roster": [{"player_id": "other"}]},
        },
    },
    "match-2": {
        "results": {
            "score": {"faction1": 10, "faction2": 13},
            "winner": "faction2",
        },
        "teams": {
            "faction1": {"roster": [{"player_id": PLAYER_ID}]},
            "faction2": {"roster": [{"player_id": "other"}]},
        },
    },
}

RANKING_COUNTRY = {"position": 12}
RANKING_REGION = {"position": 345}


class DummySession(BaseSession):
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


class FakeFaceitClient:
    def __init__(
        self,
        *,
        player: dict[str, Any] | None = PLAYER,
        lifetime: dict[str, Any] | None = LIFETIME,
        recent: dict[str, Any] | None = RECENT,
        country_ranking: dict[str, Any] | None = RANKING_COUNTRY,
        region_ranking: dict[str, Any] | None = RANKING_REGION,
        match_details: dict[str, dict[str, Any]] | None = None,
        ranking_error: Exception | None = None,
    ) -> None:
        self.player = player
        self.lifetime = lifetime
        self.recent = recent
        self.country_ranking = country_ranking
        self.region_ranking = region_ranking
        self.match_details = (
            MATCH_DETAILS if match_details is None else match_details
        )
        self.ranking_error = ranking_error
        self.cache_cleared = False
        self.recent_limits: list[int] = []
        self.ranking_calls: list[tuple[str, str, str | None]] = []
        self.requested_match_ids: list[str] = []

    async def get_player_by_steam_id(
        self,
        steam64: str,
    ) -> dict[str, Any] | None:
        return self.player

    async def get_lifetime_stats(
        self,
        player_id: str,
    ) -> dict[str, Any] | None:
        return self.lifetime

    async def get_recent_matches(
        self,
        player_id: str,
        limit: int = 20,
    ) -> dict[str, Any] | None:
        self.recent_limits.append(limit)
        return self.recent

    async def get_player_ranking(
        self,
        player_id: str,
        region: str,
        *,
        country: str | None = None,
    ) -> dict[str, Any] | None:
        self.ranking_calls.append((player_id, region, country))
        if self.ranking_error is not None:
            raise self.ranking_error
        if country is not None:
            return self.country_ranking
        return self.region_ranking

    async def get_match_details(
        self,
        match_id: str,
    ) -> dict[str, Any] | None:
        self.requested_match_ids.append(match_id)
        return self.match_details.get(match_id)

    def clear_cache(self) -> None:
        self.cache_cleared = True


def patch_steam(
    monkeypatch: pytest.MonkeyPatch,
    steam_stats: SteamStats | None = None,
    *,
    error: Exception | None = None,
) -> None:
    """Подменяет загрузку Steam, чтобы тесты не ходили в сеть."""

    async def fake_fetch_steam_stats(steam64: str) -> SteamStats | None:
        assert steam64 == STEAM_ID64
        if error is not None:
            raise error
        return steam_stats

    monkeypatch.setattr(handlers, "fetch_steam_stats", fake_fetch_steam_stats)


@pytest_asyncio.fixture
async def bot() -> AsyncGenerator[Bot, None]:
    bot = Bot(token="42:TEST", session=DummySession())
    yield bot
    await bot.session.close()


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    """Один диспетчер на модуль: aiogram запрещает повторно подключать
    глобальный роутер к разным диспетчерам."""
    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.router)
    return dispatcher


def make_message(text: str, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=CHAT,
        from_user=USER,
        text=text,
    )


def send_messages(bot: Bot) -> list[SendMessage]:
    return [
        method
        for method in bot.session.requests
        if isinstance(method, SendMessage)
    ]


def setup_dispatcher(
    dispatcher: Dispatcher,
    faceit_client: FakeFaceitClient | None,
) -> None:
    dispatcher["faceit_client"] = faceit_client


@pytest.mark.asyncio
async def test_handle_link_sends_stats_and_keyboard(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = FakeFaceitClient()
    setup_dispatcher(dispatcher, faceit_client)
    patch_steam(
        monkeypatch,
        SteamStats(cs2_hours=100, vac_banned=False),
    )

    async def fake_resolve(url: str) -> str:
        assert url == "https://steamcommunity.com/id/test"
        return STEAM_ID64

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve)

    update = Update(
        update_id=1,
        message=make_message(
            "Мой профиль: https://steamcommunity.com/id/test"
        ),
    )
    await dispatcher.feed_update(bot, update)

    messages = send_messages(bot)
    assert len(messages) == 1
    sent = messages[0]
    text = sent.text or ""

    assert "TestPlayer" in text
    assert "🏅 Уровень 10 · 2345 ELO" in text
    assert "Максимальный ELO: 2600" in text
    assert "• Страна: 🇰🇿 KZ" in text
    assert "• Регион: EU" in text
    assert "• Винрейт: 55%" in text
    assert "• K/D: 1.25" in text
    assert "• HS%: 48%" in text
    assert "• Текущая серия: 1 победа" in text
    assert "• Результаты последних 10 матчей: ✅❌" in text
    assert "• de_mirage · 13:9 · 20/10/5 · +20 ELO" in text
    assert "• de_dust2 · 10:13 · 10/20/2\n" in text
    assert "💻 <b>Steam</b>" in text
    assert "• CS2: 100 ч" in text
    assert "• VAC: ✅ нет" in text
    assert "Обновлено" in text

    assert faceit_client.recent_limits == [RECENT_MATCHES_FETCH_LIMIT]
    assert faceit_client.requested_match_ids == ["match-1", "match-2"]
    assert set(faceit_client.ranking_calls) == {
        (PLAYER_ID, "EU", None),
        (PLAYER_ID, "EU", "kz"),
    }

    assert sent.reply_markup is not None

    keyboard = sent.reply_markup.inline_keyboard
    assert keyboard[0][0].text == "Открыть профиль на Faceit"
    assert keyboard[0][0].url == (
        "https://www.faceit.com/ru/players/TestPlayer"
    )
    assert keyboard[1][0].text == "Обновить"
    assert keyboard[1][0].callback_data == f"refresh:{STEAM_ID64}"


@pytest.mark.asyncio
async def test_handle_link_reports_missing_faceit_player(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient(player=None))
    patch_steam(monkeypatch)

    async def fake_resolve(url: str) -> str:
        return STEAM_ID64

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message("https://steamcommunity.com/id/test"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == "У этого игрока нет Faceit-аккаунта с CS2"


@pytest.mark.asyncio
async def test_handle_link_reports_invalid_steam_link(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())
    patch_steam(monkeypatch)

    async def fake_resolve(url: str) -> None:
        return None

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message("https://example.com/not-steam"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.INVALID_STEAM_MESSAGE


@pytest.mark.asyncio
async def test_handle_link_without_faceit_configuration(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, None)

    async def fail_on_resolve(url: str) -> str:
        pytest.fail("Steam-резолвер не должен вызываться без Faceit-клиента")

    monkeypatch.setattr(handlers, "resolve_steam_id", fail_on_resolve)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message("https://steamcommunity.com/id/test"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.FACEIT_NOT_CONFIGURED_MESSAGE


@pytest.mark.asyncio
async def test_handle_link_survives_optional_ranking_and_steam_errors(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(
        dispatcher,
        FakeFaceitClient(ranking_error=handlers.FaceitError("rankings down")),
    )
    patch_steam(monkeypatch, error=RuntimeError("steam down"))

    async def fake_resolve(url: str) -> str:
        return STEAM_ID64

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message("https://steamcommunity.com/id/test"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    text = messages[0].text or ""
    assert "TestPlayer" in text
    assert "Рейтинг" not in text
    assert "Steam" not in text


@pytest.mark.asyncio
async def test_refresh_callback_clears_cache_and_edits_message(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = FakeFaceitClient()
    setup_dispatcher(dispatcher, faceit_client)
    patch_steam(
        monkeypatch,
        SteamStats(cs2_hours=100, vac_banned=False),
    )

    old_message = make_message("старое сообщение", message_id=2)
    callback = CallbackQuery(
        id="callback-1",
        from_user=USER,
        chat_instance="chat-instance",
        data=f"refresh:{STEAM_ID64}",
        message=old_message,
    )

    await dispatcher.feed_update(
        bot,
        Update(update_id=2, callback_query=callback),
    )

    assert faceit_client.cache_cleared is True

    answers = [
        method
        for method in bot.session.requests
        if isinstance(method, AnswerCallbackQuery)
    ]
    assert answers
    assert answers[0].text == "Обновляю статистику…"

    edits = [
        method
        for method in bot.session.requests
        if isinstance(method, EditMessageText)
    ]
    assert len(edits) == 1
    assert "TestPlayer" in (edits[0].text or "")
    assert "Уровень 10 · 2345 ELO" in (edits[0].text or "")
    assert edits[0].chat_id == CHAT.id
    assert edits[0].message_id == old_message.message_id
    assert edits[0].reply_markup is not None
    assert (
        edits[0].reply_markup.inline_keyboard[1][0].callback_data
        == f"refresh:{STEAM_ID64}"
    )
