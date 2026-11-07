from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    EditMessageText,
    AnswerCallbackQuery,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot import handlers
from bot.formatter import (
    MENU_CANCEL_CALLBACK,
    MENU_COMPARE_CALLBACK,
    MENU_COMPARE_TEXT,
    MENU_FIND_CALLBACK,
    MENU_FIND_TEXT,
    MENU_HELP_TEXT,
)
from bot.steam import SteamStats
from bot.stats import RECENT_MATCHES_FETCH_LIMIT

STEAM_ID64 = "76561198000000000"
STEAM_ID64_TWO = "76561198000000001"
PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"
PLAYER_TWO_ID = "9a4b1c2d-3e4f-5a6b-7c8d-9e0f1a2b3c4d"
CHAT = Chat(id=100, type="private")
USER = User(id=100, is_bot=False, first_name="Tester")

URL_ONE = "https://steamcommunity.com/id/test"
URL_TWO = "https://steamcommunity.com/id/rival"

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

PLAYER_TWO = {
    "player_id": PLAYER_TWO_ID,
    "nickname": "RivalPlayer",
    "country": "ru",
    "faceit_url": "https://www.faceit.com/ru/players/RivalPlayer",
    "games": {
        "cs2": {
            "region": "EU",
            "faceit_elo": 2100,
            "skill_level": 9,
        }
    },
}

LIFETIME_TWO = {
    "player_id": PLAYER_TWO_ID,
    "lifetime": {
        "Matches": "100",
        "Win Rate %": "60",
        "Average K/D Ratio": "1.10",
        "Average Headshots %": "52",
        "Longest Win Streak": "8",
        "Current Win Streak": "0",
    },
    "segments": [],
}

RECENT_TWO = {
    "player_id": PLAYER_TWO_ID,
    "items": [
        {
            "match_id": "match-3",
            "elo": 2100,
            "stats": {
                "Result": "0",
                "K/D Ratio": "0.8",
                "ADR": "70.2",
                "Kills": "12",
                "Deaths": "15",
                "Assists": "4",
                "Map": "de_nuke",
            },
        },
        {
            "match_id": "match-4",
            "elo": 2110,
            "stats": {
                "Result": "1",
                "K/D Ratio": "1.4",
                "ADR": "75.0",
                "Kills": "21",
                "Deaths": "15",
                "Assists": "6",
                "Map": "de_ancient",
            },
        },
    ],
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
        players_by_steam: dict[str, dict[str, Any] | None] | None = None,
        lifetime_by_player: (
            dict[str, dict[str, Any] | None] | None
        ) = None,
        recent_by_player: dict[str, dict[str, Any] | None] | None = None,
        player_errors: dict[str, Exception] | None = None,
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
        self.players_by_steam = players_by_steam
        self.lifetime_by_player = lifetime_by_player
        self.recent_by_player = recent_by_player
        self.player_errors = player_errors
        self.cache_cleared = False
        self.recent_limits: list[int] = []
        self.ranking_calls: list[tuple[str, str, str | None]] = []
        self.requested_match_ids: list[str] = []

    async def get_player_by_steam_id(
        self,
        steam64: str,
    ) -> dict[str, Any] | None:
        if self.player_errors is not None and steam64 in self.player_errors:
            raise self.player_errors[steam64]
        if self.players_by_steam is not None:
            return self.players_by_steam.get(steam64)
        return self.player

    async def get_lifetime_stats(
        self,
        player_id: str,
    ) -> dict[str, Any] | None:
        if self.lifetime_by_player is not None:
            return self.lifetime_by_player.get(player_id)
        return self.lifetime

    async def get_recent_matches(
        self,
        player_id: str,
        limit: int = 20,
    ) -> dict[str, Any] | None:
        self.recent_limits.append(limit)
        if self.recent_by_player is not None:
            return self.recent_by_player.get(player_id)
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


def make_compare_client(
    *,
    players_by_steam: dict[str, dict[str, Any] | None] | None = None,
    lifetime_by_player: (
        dict[str, dict[str, Any] | None] | None
    ) = None,
    recent_by_player: dict[str, dict[str, Any] | None] | None = None,
    player_errors: dict[str, Exception] | None = None,
) -> FakeFaceitClient:
    return FakeFaceitClient(
        players_by_steam=players_by_steam,
        lifetime_by_player=lifetime_by_player,
        recent_by_player=recent_by_player,
        player_errors=player_errors,
    )


def patch_compare_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    steam_by_steam: dict[str, SteamStats | None] | None = None,
) -> None:
    """Мокает Steam и SteamID-резолвер для сценария сравнения."""

    async def fake_resolve_steam_id(value: str) -> str | None:
        return {
            URL_ONE: STEAM_ID64,
            URL_TWO: STEAM_ID64_TWO,
        }.get(value)

    async def fake_fetch_steam_stats(steam64: str) -> SteamStats | None:
        if steam_by_steam is None:
            return None
        return steam_by_steam.get(steam64)

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve_steam_id)
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


def fsm_key(bot: Bot) -> StorageKey:
    """Возвращает ключ FSM для тестового чата и пользователя."""
    return StorageKey(
        bot_id=bot.id,
        chat_id=CHAT.id,
        user_id=USER.id,
    )


async def clear_fsm(dispatcher: Dispatcher, bot: Bot) -> None:
    """Полностью сбрасывает состояние и данные FSM."""
    key = fsm_key(bot)
    await dispatcher.storage.set_state(key, None)
    await dispatcher.storage.set_data(key, {})


async def get_fsm_state(dispatcher: Dispatcher, bot: Bot) -> str | None:
    """Возвращает текущее состояние FSM."""
    context = FSMContext(dispatcher.storage, fsm_key(bot))
    return await context.get_state()


@pytest_asyncio.fixture(autouse=True)
async def reset_fsm(
    dispatcher: Dispatcher,
    bot: Bot,
) -> AsyncGenerator[None, None]:
    """Изолирует FSM между интеграционными тестами."""
    await clear_fsm(dispatcher, bot)
    yield
    await clear_fsm(dispatcher, bot)


def make_message(text: str, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=CHAT,
        from_user=USER,
        text=text,
    )


def make_callback(data: str, message_id: int = 2) -> CallbackQuery:
    return CallbackQuery(
        id="callback-1",
        from_user=USER,
        chat_instance="chat-instance",
        data=data,
        message=make_message("меню", message_id=message_id),
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

@pytest.mark.asyncio
async def test_handle_compare_command_sends_comparison(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = make_compare_client(
        players_by_steam={
            STEAM_ID64: PLAYER,
            STEAM_ID64_TWO: PLAYER_TWO,
        },
        lifetime_by_player={
            PLAYER_ID: LIFETIME,
            PLAYER_TWO_ID: LIFETIME_TWO,
        },
        recent_by_player={
            PLAYER_ID: RECENT,
            PLAYER_TWO_ID: RECENT_TWO,
        },
    )
    setup_dispatcher(dispatcher, faceit_client)
    patch_compare_dependencies(
        monkeypatch,
        steam_by_steam={
            STEAM_ID64: SteamStats(cs2_hours=100, vac_banned=False),
            STEAM_ID64_TWO: SteamStats(cs2_hours=200, vac_banned=False),
        },
    )

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(f"/compare {URL_ONE} {URL_TWO}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    sent = messages[0]
    text = sent.text or ""

    assert "⚔️ <b>Сравнение игроков</b>" in text
    assert "TestPlayer" in text
    assert "RivalPlayer" in text
    assert "• ELO: <b>2345</b> 🏆 vs 2100 · Δ +245" in text
    assert "• Винрейт: 55% vs <b>60%</b> 🏆 · Δ −5 п.п." in text
    assert "• K/D: <b>1.25</b> 🏆 vs 1.10 · Δ +0.15" in text
    assert "• CS2: 100 ч vs 200 ч · Δ −100 ч" in text
    assert "Обновлено" in text

    assert faceit_client.recent_limits == [
        RECENT_MATCHES_FETCH_LIMIT,
        RECENT_MATCHES_FETCH_LIMIT,
    ]

    assert sent.reply_markup is not None
    keyboard = sent.reply_markup.inline_keyboard
    assert keyboard[0][0].text == "1. TestPlayer"
    assert keyboard[0][1].text == "2. RivalPlayer"
    assert keyboard[1][0].text == "🔄 Обновить сравнение"
    assert keyboard[1][0].callback_data == (
        f"refresh_compare:{STEAM_ID64}:{STEAM_ID64_TWO}"
    )


@pytest.mark.asyncio
async def test_handle_link_with_two_urls_starts_comparison(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = make_compare_client(
        players_by_steam={
            STEAM_ID64: PLAYER,
            STEAM_ID64_TWO: PLAYER_TWO,
        },
        lifetime_by_player={
            PLAYER_ID: LIFETIME,
            PLAYER_TWO_ID: LIFETIME_TWO,
        },
        recent_by_player={
            PLAYER_ID: RECENT,
            PLAYER_TWO_ID: RECENT_TWO,
        },
    )
    setup_dispatcher(dispatcher, faceit_client)
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(f"Сравни: {URL_ONE} и {URL_TWO}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert "Сравнение игроков" in (messages[0].text or "")


@pytest.mark.asyncio
async def test_handle_compare_usage_for_missing_second_link(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, make_compare_client())
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message("/compare")),
    )
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            message=make_message(f"/compare {URL_ONE}"),
        ),
    )

    messages = send_messages(bot)
    assert [message.text for message in messages] == [
        handlers.COMPARE_USAGE_MESSAGE,
        handlers.COMPARE_ONE_LINK_MESSAGE,
    ]


@pytest.mark.asyncio
async def test_handle_compare_reports_same_player(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, make_compare_client())
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(f"/compare {URL_ONE} {URL_ONE}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.COMPARE_SAME_PLAYER_MESSAGE


@pytest.mark.asyncio
async def test_handle_compare_reports_missing_second_player(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(
        dispatcher,
        make_compare_client(
            players_by_steam={
                STEAM_ID64: PLAYER,
                STEAM_ID64_TWO: None,
            },
            lifetime_by_player={
                PLAYER_ID: LIFETIME,
                PLAYER_TWO_ID: LIFETIME_TWO,
            },
            recent_by_player={
                PLAYER_ID: RECENT,
                PLAYER_TWO_ID: RECENT_TWO,
            },
        ),
    )
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(f"/compare {URL_ONE} {URL_TWO}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.COMPARE_SECOND_NOT_FOUND_MESSAGE


@pytest.mark.asyncio
async def test_handle_compare_reports_rate_limit_for_one_player(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(
        dispatcher,
        make_compare_client(
            players_by_steam={
                STEAM_ID64: PLAYER,
                STEAM_ID64_TWO: PLAYER_TWO,
            },
            lifetime_by_player={
                PLAYER_ID: LIFETIME,
                PLAYER_TWO_ID: LIFETIME_TWO,
            },
            recent_by_player={
                PLAYER_ID: RECENT,
                PLAYER_TWO_ID: RECENT_TWO,
            },
            player_errors={
                STEAM_ID64_TWO: handlers.FaceitRateLimit("rate limit"),
            },
        ),
    )
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(f"/compare {URL_ONE} {URL_TWO}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.FACEIT_RATE_LIMIT_MESSAGE


@pytest.mark.asyncio
async def test_handle_compare_reports_invalid_link(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, make_compare_client())

    async def fake_resolve_steam_id(value: str) -> str | None:
        return None

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve_steam_id)

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=make_message(
                "https://example.com/one https://example.com/two"
            ),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.COMPARE_INVALID_STEAM_MESSAGE


@pytest.mark.asyncio
async def test_handle_compare_refresh_clears_cache_and_edits_message(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = make_compare_client(
        players_by_steam={
            STEAM_ID64: PLAYER,
            STEAM_ID64_TWO: PLAYER_TWO,
        },
        lifetime_by_player={
            PLAYER_ID: LIFETIME,
            PLAYER_TWO_ID: LIFETIME_TWO,
        },
        recent_by_player={
            PLAYER_ID: RECENT,
            PLAYER_TWO_ID: RECENT_TWO,
        },
    )
    setup_dispatcher(dispatcher, faceit_client)
    patch_compare_dependencies(monkeypatch)

    old_message = make_message("старое сравнение", message_id=2)
    callback = CallbackQuery(
        id="callback-1",
        from_user=USER,
        chat_instance="chat-instance",
        data=(
            f"refresh_compare:{STEAM_ID64}:{STEAM_ID64_TWO}"
        ),
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
    assert answers[0].text == "Обновляю сравнение…"

    edits = [
        method
        for method in bot.session.requests
        if isinstance(method, EditMessageText)
    ]
    assert len(edits) == 1
    assert "Сравнение игроков" in (edits[0].text or "")
    assert "TestPlayer" in (edits[0].text or "")
    assert "RivalPlayer" in (edits[0].text or "")
    assert edits[0].reply_markup is not None
    assert (
        edits[0].reply_markup.inline_keyboard[1][0].callback_data
        == f"refresh_compare:{STEAM_ID64}:{STEAM_ID64_TWO}"
    )


@pytest.mark.asyncio
async def test_handle_start_shows_start_and_persistent_menu(
    bot: Bot,
    dispatcher: Dispatcher,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message("/start")),
    )

    messages = send_messages(bot)
    assert len(messages) == 2
    start_message, menu_message = messages

    assert "Привет" in (start_message.text or "")
    start_keyboard = start_message.reply_markup
    assert isinstance(start_keyboard, InlineKeyboardMarkup)
    assert start_keyboard.inline_keyboard[0][0].callback_data == (
        MENU_FIND_CALLBACK
    )
    assert start_keyboard.inline_keyboard[0][1].callback_data == (
        MENU_COMPARE_CALLBACK
    )

    menu_keyboard = menu_message.reply_markup
    assert isinstance(menu_keyboard, ReplyKeyboardMarkup)
    assert menu_keyboard.is_persistent is True
    assert menu_keyboard.resize_keyboard is True
    assert [
        [button.text for button in row]
        for row in menu_keyboard.keyboard
    ] == [
        [MENU_FIND_TEXT, MENU_COMPARE_TEXT],
        [MENU_HELP_TEXT],
    ]


@pytest.mark.asyncio
async def test_handle_menu_find_text_prompts_for_link(
    bot: Bot,
    dispatcher: Dispatcher,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_FIND_TEXT)),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert "Поиск игрока" in (messages[0].text or "")
    assert messages[0].reply_markup is not None
    assert (
        messages[0].reply_markup.inline_keyboard[0][0].callback_data
        == MENU_CANCEL_CALLBACK
    )


@pytest.mark.asyncio
async def test_guided_compare_flow_from_reply_menu(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = make_compare_client(
        players_by_steam={
            STEAM_ID64: PLAYER,
            STEAM_ID64_TWO: PLAYER_TWO,
        },
        lifetime_by_player={
            PLAYER_ID: LIFETIME,
            PLAYER_TWO_ID: LIFETIME_TWO,
        },
        recent_by_player={
            PLAYER_ID: RECENT,
            PLAYER_TWO_ID: RECENT_TWO,
        },
    )
    setup_dispatcher(dispatcher, faceit_client)
    patch_compare_dependencies(
        monkeypatch,
        steam_by_steam={
            STEAM_ID64: SteamStats(cs2_hours=100, vac_banned=False),
            STEAM_ID64_TWO: SteamStats(cs2_hours=200, vac_banned=False),
        },
    )

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_COMPARE_TEXT)),
    )

    first_prompt = send_messages(bot)[0]
    assert "Шаг 1 из 2" in (first_prompt.text or "")
    assert (
        first_prompt.reply_markup.inline_keyboard[0][0].callback_data
        == MENU_CANCEL_CALLBACK
    )
    assert await get_fsm_state(dispatcher, bot) == (
        handlers.CompareFlow.waiting_first.state
    )

    bot.session.requests.clear()
    await dispatcher.feed_update(
        bot,
        Update(update_id=2, message=make_message(URL_ONE)),
    )

    second_prompt = send_messages(bot)[0]
    assert "Шаг 2 из 2" in (second_prompt.text or "")
    assert "TestPlayer" in (second_prompt.text or "")
    assert await get_fsm_state(dispatcher, bot) == (
        handlers.CompareFlow.waiting_second.state
    )

    bot.session.requests.clear()
    await dispatcher.feed_update(
        bot,
        Update(update_id=3, message=make_message(URL_TWO)),
    )

    comparison_messages = send_messages(bot)
    assert len(comparison_messages) == 1
    comparison = comparison_messages[0]
    assert "Сравнение игроков" in (comparison.text or "")
    assert "TestPlayer" in (comparison.text or "")
    assert "RivalPlayer" in (comparison.text or "")
    assert comparison.reply_markup is not None
    assert (
        comparison.reply_markup.inline_keyboard[1][0].callback_data
        == f"refresh_compare:{STEAM_ID64}:{STEAM_ID64_TWO}"
    )
    assert await get_fsm_state(dispatcher, bot) is None


@pytest.mark.asyncio
async def test_guided_compare_accepts_two_links_at_first_step(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faceit_client = make_compare_client(
        players_by_steam={
            STEAM_ID64: PLAYER,
            STEAM_ID64_TWO: PLAYER_TWO,
        },
        lifetime_by_player={
            PLAYER_ID: LIFETIME,
            PLAYER_TWO_ID: LIFETIME_TWO,
        },
        recent_by_player={
            PLAYER_ID: RECENT,
            PLAYER_TWO_ID: RECENT_TWO,
        },
    )
    setup_dispatcher(dispatcher, faceit_client)
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_COMPARE_TEXT)),
    )
    bot.session.requests.clear()

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            message=make_message(f"{URL_ONE} {URL_TWO}"),
        ),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert "Сравнение игроков" in (messages[0].text or "")
    assert await get_fsm_state(dispatcher, bot) is None


@pytest.mark.asyncio
async def test_compare_cancel_callback_clears_state(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_COMPARE_TEXT)),
    )
    assert await get_fsm_state(dispatcher, bot) == (
        handlers.CompareFlow.waiting_first.state
    )

    bot.session.requests.clear()
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            callback_query=make_callback(MENU_CANCEL_CALLBACK),
        ),
    )

    assert await get_fsm_state(dispatcher, bot) is None

    answers = [
        method
        for method in bot.session.requests
        if isinstance(method, AnswerCallbackQuery)
    ]
    assert answers[0].text == "Отменено"

    edits = [
        method
        for method in bot.session.requests
        if isinstance(method, EditMessageText)
    ]
    assert len(edits) == 1
    assert edits[0].text == handlers.CANCELLED_MESSAGE
    assert edits[0].reply_markup is not None
    assert (
        edits[0].reply_markup.inline_keyboard[0][0].callback_data
        == MENU_FIND_CALLBACK
    )


@pytest.mark.asyncio
async def test_menu_compare_callback_starts_guided_flow(
    bot: Bot,
    dispatcher: Dispatcher,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            callback_query=make_callback(MENU_COMPARE_CALLBACK),
        ),
    )

    assert await get_fsm_state(dispatcher, bot) == (
        handlers.CompareFlow.waiting_first.state
    )

    edits = [
        method
        for method in bot.session.requests
        if isinstance(method, EditMessageText)
    ]
    assert len(edits) == 1
    assert "Шаг 1 из 2" in (edits[0].text or "")
    assert edits[0].reply_markup is not None
    assert (
        edits[0].reply_markup.inline_keyboard[0][0].callback_data
        == MENU_CANCEL_CALLBACK
    )


@pytest.mark.asyncio
async def test_cancel_command_clears_guided_flow(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())
    patch_compare_dependencies(monkeypatch)

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_COMPARE_TEXT)),
    )
    bot.session.requests.clear()

    await dispatcher.feed_update(
        bot,
        Update(update_id=2, message=make_message("/cancel")),
    )

    assert await get_fsm_state(dispatcher, bot) is None
    messages = send_messages(bot)
    assert len(messages) == 1
    assert messages[0].text == handlers.CANCELLED_MESSAGE
    assert messages[0].reply_markup is not None
    assert (
        messages[0].reply_markup.inline_keyboard[0][0].callback_data
        == MENU_FIND_CALLBACK
    )


@pytest.mark.asyncio
async def test_find_menu_then_single_link_shows_stats(
    bot: Bot,
    dispatcher: Dispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dispatcher(dispatcher, FakeFaceitClient())
    patch_steam(
        monkeypatch,
        SteamStats(cs2_hours=100, vac_banned=False),
    )

    async def fake_resolve(value: str) -> str:
        assert value == URL_ONE
        return STEAM_ID64

    monkeypatch.setattr(handlers, "resolve_steam_id", fake_resolve)

    await dispatcher.feed_update(
        bot,
        Update(update_id=1, message=make_message(MENU_FIND_TEXT)),
    )
    bot.session.requests.clear()

    await dispatcher.feed_update(
        bot,
        Update(update_id=2, message=make_message(URL_ONE)),
    )

    messages = send_messages(bot)
    assert len(messages) == 1
    assert "TestPlayer" in (messages[0].text or "")
    assert "Общая статистика" in (messages[0].text or "")
    assert await get_fsm_state(dispatcher, bot) is None
