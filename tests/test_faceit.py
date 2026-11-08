from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from bot.faceit import (
    CACHE_TTL_SECONDS,
    FACEIT_API_BASE_URL,
    FaceitClient,
    FaceitError,
    FaceitNotFound,
    FaceitRateLimit,
    _TTLCache,
    _TokenBucket,
)

STEAM_ID64 = "76561198000000000"
STEAM_ID64_TWO = "76561198000000001"
PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"
PLAYER_TWO_ID = "9a4b1c2d-3e4f-5a6b-7c8d-9e0f1a2b3c4d"

PLAYER_PAYLOAD: dict[str, Any] = {
    "player_id": PLAYER_ID,
    "nickname": "TestPlayer",
    "country": "kz",
    "avatar": "https://example.com/avatar.png",
    "faceit_url": "https://www.faceit.com/ru/players/TestPlayer",
    "games": {
        "cs2": {
            "region": "EU",
            "game_player_id": STEAM_ID64,
            "game_player_name": "TestPlayer",
            "skill_level": 10,
            "skill_level_label": "Level 10",
            "faceit_elo": 2345,
        }
    },
}

PLAYER_TWO_PAYLOAD: dict[str, Any] = {
    "player_id": PLAYER_TWO_ID,
    "nickname": "RivalPlayer",
    "country": "ru",
    "games": {
        "cs2": {
            "region": "EU",
            "game_player_id": STEAM_ID64_TWO,
            "game_player_name": "RivalPlayer",
            "skill_level": 9,
            "faceit_elo": 2100,
        }
    },
}

LIFETIME_PAYLOAD: dict[str, Any] = {
    "player_id": PLAYER_ID,
    "game_id": "cs2",
    "lifetime": {
        "Matches": "120",
        "K/D Ratio": "1.25",
        "Win Rate %": "54",
    },
    "segments": [
        {
            "type": "Map",
            "label": "de_dust2",
            "mode": "5v5",
            "stats": {"Matches": "20", "Wins": "11"},
        }
    ],
}

RECENT_MATCHES_PAYLOAD: dict[str, Any] = {
    "player_id": PLAYER_ID,
    "game_id": "cs2",
    "items": [
        {
            "stats": {
                "Map": "de_mirage",
                "K/D Ratio": "1.5",
                "Result": "1",
            },
            "created_at": 1720000000,
        }
    ],
}

RANKING_PAYLOAD: dict[str, Any] = {
    "position": 42,
    "start": 1,
    "end": 50,
    "items": [],
}

MATCH_DETAILS_PAYLOAD: dict[str, Any] = {
    "match_id": "match-1",
    "results": {
        "score": {"faction1": 13, "faction2": 9},
        "winner": "faction1",
    },
    "teams": {
        "faction1": {"roster": [{"player_id": PLAYER_ID}]},
        "faction2": {"roster": [{"player_id": "other"}]},
    },
}


def make_handler(
    response: httpx.Response | None = None,
    *,
    responses: list[httpx.Response] | None = None,
    captured: list[httpx.Request] | None = None,
) -> Any:
    """Создаёт sync-обработчик для httpx.MockTransport."""
    queue = list(responses or ([] if response is None else [response]))

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    return handler


class AsyncMockTransport(httpx.AsyncBaseTransport):
    """Асинхронный транспорт для проверки конкурентных сценариев."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
    ) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.requests.append(request)
        response = await self._handler(request)
        response.request = request
        return response


@pytest.mark.asyncio
async def test_get_player_by_steam_id_parses_response() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=PLAYER_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        player = await client.get_player_by_steam_id(STEAM_ID64)

    assert player is not None
    assert player["player_id"] == PLAYER_ID
    assert player["nickname"] == "TestPlayer"
    assert player["country"] == "kz"
    assert player["avatar"] == "https://example.com/avatar.png"
    assert player["faceit_url"] == "https://www.faceit.com/ru/players/TestPlayer"
    assert player["games"]["cs2"]["faceit_elo"] == 2345
    assert player["games"]["cs2"]["skill_level"] == 10
    assert player["region"] == "EU"

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == (
        f"{FACEIT_API_BASE_URL}/players"
        f"?game=cs2&game_player_id={STEAM_ID64}"
    )
    assert request.headers["Authorization"] == "Bearer test-api-key"


@pytest.mark.asyncio
async def test_get_player_returns_none_on_404() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(404, json={"message": "Not found"}))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        player = await client.get_player_by_steam_id(STEAM_ID64)

    assert player is None


@pytest.mark.asyncio
async def test_get_lifetime_stats_parses_lifetime_and_segments() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=LIFETIME_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        stats = await client.get_lifetime_stats(PLAYER_ID)

    assert stats["player_id"] == PLAYER_ID
    assert stats["lifetime"]["K/D Ratio"] == "1.25"
    assert stats["segments"][0]["label"] == "de_dust2"
    assert stats["segments"][0]["stats"]["Wins"] == "11"
    assert captured[0].url.path == (
        f"/data/v4/players/{PLAYER_ID}/stats/cs2"
    )


@pytest.mark.asyncio
async def test_get_lifetime_stats_raises_not_found() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(404, json={"message": "Not found"}))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        with pytest.raises(FaceitNotFound):
            await client.get_lifetime_stats(PLAYER_ID)


@pytest.mark.asyncio
async def test_get_recent_matches_parses_items_and_sends_limit() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=RECENT_MATCHES_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        matches = await client.get_recent_matches(PLAYER_ID, limit=5)

    assert matches["items"][0]["stats"]["Map"] == "de_mirage"
    assert matches["items"][0]["created_at"] == 1720000000
    assert captured[0].url.path == (
        f"/data/v4/players/{PLAYER_ID}/games/cs2/stats"
    )
    assert captured[0].url.params["limit"] == "5"


@pytest.mark.asyncio
async def test_get_recent_matches_rejects_invalid_limit() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(200, json=RECENT_MATCHES_PAYLOAD))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        with pytest.raises(ValueError):
            await client.get_recent_matches(PLAYER_ID, limit=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_status", [500, 503])
async def test_retries_on_5xx(retry_status: int) -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            responses=[
                httpx.Response(retry_status),
                httpx.Response(200, json=PLAYER_PAYLOAD),
            ],
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=1,
        backoff_base=0,
        transport=transport,
    ) as client:
        player = await client.get_player_by_steam_id(STEAM_ID64)

    assert player is not None
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_429_is_not_retried_even_with_retries_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(
                429,
                headers={"Retry-After": "1"},
            ),
            captured=captured,
        )
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async with FaceitClient(
        "test-api-key",
        max_retries=5,
        backoff_base=0,
        rate_limit_cooldown=30.0,
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit) as exc_info:
            await client.get_player_by_steam_id(STEAM_ID64)

    assert exc_info.value.retry_after == pytest.approx(30.0)
    assert len(captured) == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_5xx_retries_are_exhausted() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(500),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=2,
        backoff_base=0,
        transport=transport,
    ) as client:
        with pytest.raises(FaceitError):
            await client.get_player_by_steam_id(STEAM_ID64)

    assert len(captured) == 3


@pytest.mark.asyncio
async def test_429_does_not_retry_after_first_response() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(429),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=2,
        rate_limit_cooldown=0.0,
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit):
            await client.get_player_by_steam_id(STEAM_ID64)

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_does_not_retry_client_errors() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(400, json={"message": "Bad request"}),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=3,
        backoff_base=0,
        transport=transport,
    ) as client:
        with pytest.raises(FaceitError):
            await client.get_player_by_steam_id(STEAM_ID64)

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_network_error_becomes_faceit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    transport = httpx.MockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        with pytest.raises(FaceitError):
            await client.get_lifetime_stats(PLAYER_ID)


@pytest.mark.asyncio
async def test_invalid_json_becomes_faceit_error() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(200, content=b"not json"))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        with pytest.raises(FaceitError):
            await client.get_lifetime_stats(PLAYER_ID)


@pytest.mark.asyncio
async def test_invalid_response_schema_becomes_faceit_error() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(200, json={"unexpected": True}))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        with pytest.raises(FaceitError):
            await client.get_player_by_steam_id(STEAM_ID64)


@pytest.mark.asyncio
async def test_player_response_is_cached() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=PLAYER_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        first = await client.get_player_by_steam_id(STEAM_ID64)
        second = await client.get_player_by_steam_id(STEAM_ID64)
        assert len(captured) == 1
        assert first == second

        client.clear_cache()
        await client.get_player_by_steam_id(STEAM_ID64)
        assert len(captured) == 2


def test_ttl_cache_expires() -> None:
    now = 100.0
    cache = _TTLCache[str, int](
        CACHE_TTL_SECONDS,
        clock=lambda: now,
    )

    cache.set("key", 42)
    assert cache.get("key") == 42

    now += CACHE_TTL_SECONDS - 1
    assert cache.get("key") == 42

    now += 1
    assert cache.get("key") is None


@pytest.mark.asyncio
async def test_get_player_ranking_uses_region_and_country() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=RANKING_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        region = await client.get_player_ranking(PLAYER_ID, "EU")
        country = await client.get_player_ranking(
            PLAYER_ID,
            "EU",
            country="kz",
        )
        cached_region = await client.get_player_ranking(PLAYER_ID, "EU")

    assert region is not None
    assert region["position"] == 42
    assert country is not None
    assert country["position"] == 42
    assert cached_region == region

    assert len(captured) == 2
    assert captured[0].url.path == (
        f"/data/v4/rankings/games/cs2/regions/EU/players/{PLAYER_ID}"
    )
    assert "country" not in captured[0].url.params
    assert captured[1].url.params["country"] == "kz"


@pytest.mark.asyncio
async def test_get_player_ranking_returns_none_on_404() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(404, json={"message": "Not found"}))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        assert await client.get_player_ranking(PLAYER_ID, "EU") is None


@pytest.mark.asyncio
async def test_get_match_details_parses_score_and_teams() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=MATCH_DETAILS_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        details = await client.get_match_details("match-1")
        cached = await client.get_match_details("match-1")

    assert details is not None
    assert details["match_id"] == "match-1"
    assert details["results"]["score"] == {
        "faction1": 13,
        "faction2": 9,
    }
    assert details["teams"]["faction1"]["roster"][0]["player_id"] == PLAYER_ID
    assert cached == details

    assert len(captured) == 1
    assert captured[0].url.path == "/data/v4/matches/match-1"


@pytest.mark.asyncio
async def test_get_match_details_returns_none_on_404() -> None:
    transport = httpx.MockTransport(
        make_handler(response=httpx.Response(404, json={"message": "Not found"}))
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        assert await client.get_match_details("missing") is None


@pytest.mark.asyncio
async def test_invalidate_player_keeps_completed_match_details() -> None:
    """Завершённый матч неизменяем: refresh игрока не сбрасывает его детали."""
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(200, json=MATCH_DETAILS_PAYLOAD),
            captured=captured,
        )
    )

    async with FaceitClient("test-api-key", transport=transport) as client:
        first = await client.get_match_details("match-1")
        client.invalidate_player(PLAYER_ID)
        second = await client.get_match_details("match-1")

    assert first == second
    assert len(captured) == 1


def test_ttl_cache_evicts_least_recently_used_when_full() -> None:
    cache = _TTLCache[str, int](
        CACHE_TTL_SECONDS,
        max_entries=2,
    )

    cache.set("first", 1)
    cache.set("second", 2)
    # Обращение к first делает second самым старым (LRU), а не first.
    assert cache.get("first") == 1
    cache.set("third", 3)

    assert cache.get("second") is None
    assert cache.get("first") == 1
    assert cache.get("third") == 3
    assert len(cache) == 2


def test_ttl_cache_purges_expired_entries_before_eviction() -> None:
    now = [0.0]
    cache = _TTLCache[str, int](
        ttl=10,
        max_entries=2,
        clock=lambda: now[0],
    )

    cache.set("first", 1)
    cache.set("second", 2)
    now[0] = 11.0
    cache.set("third", 3)

    assert len(cache) == 1
    assert cache.get("first") is None
    assert cache.get("second") is None
    assert cache.get("third") == 3


def test_ttl_cache_purges_periodically_not_on_every_set() -> None:
    """Очистка идёт раз в purge_interval операций, но протухшее не копится."""
    now = [0.0]
    cache = _TTLCache[str, int](
        ttl=10,
        clock=lambda: now[0],
        purge_interval=3,
    )
    purge_calls: list[float] = []
    original_purge = cache._purge_expired

    def tracking_purge(current: float) -> None:
        purge_calls.append(current)
        original_purge(current)

    cache._purge_expired = tracking_purge  # type: ignore[method-assign]

    cache.set("first", 1)
    cache.set("second", 2)
    assert purge_calls == []
    assert len(cache) == 2

    now[0] = 11.0
    cache.set("third", 3)

    # Третья операция запускает плановую очистку: протухшие записи удалены.
    assert len(purge_calls) == 1
    assert len(cache) == 1
    assert cache.get("first") is None
    assert cache.get("second") is None
    assert cache.get("third") == 3


@pytest.mark.asyncio
async def test_token_bucket_waits_with_fake_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bucket = _TokenBucket(2.0, clock=lambda: now[0], capacity=2.0)

    first_wait = await bucket.acquire()
    second_wait = await bucket.acquire()
    third_wait = await bucket.acquire()

    assert first_wait == pytest.approx(0.0)
    assert second_wait == pytest.approx(0.0)
    assert third_wait == pytest.approx(0.5)
    assert sleeps == [pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_cache_hit_does_not_consume_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stats/cs2"):
            return httpx.Response(200, json=LIFETIME_PAYLOAD)
        return httpx.Response(200, json=RECENT_MATCHES_PAYLOAD)

    async with FaceitClient(
        "test-api-key",
        max_requests_per_second=1.0,
        clock=lambda: now[0],
        transport=httpx.MockTransport(handler),
    ) as client:
        stats = await client.get_lifetime_stats(PLAYER_ID)
        assert client._token_bucket.tokens == pytest.approx(0.0)

        # Повторный запрос обслуживается кэшем и не тратит токен.
        cached = await client.get_lifetime_stats(PLAYER_ID)
        assert cached == stats
        assert client._token_bucket.tokens == pytest.approx(0.0)
        assert sleeps == []

        # Новый сетевой запрос обязан дождаться пополнения ведра.
        await client.get_recent_matches(PLAYER_ID)

    assert sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_aclose_cancels_and_awaits_inflight_requests() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    transport = AsyncMockTransport(handler)
    client = FaceitClient("test-api-key", transport=transport)

    task = asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
    await started.wait()

    await client.aclose()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert client._inflight == {}


def test_ttl_cache_invalidate_tag_keeps_other_entries() -> None:
    cache = _TTLCache[str, int](CACHE_TTL_SECONDS)

    cache.set("a1", 1, tags=("player:a",))
    cache.set("a2", 2, tags=("player:a", "steam:a"))
    cache.set("b1", 3, tags=("player:b", "steam:b"))

    cache.invalidate_tag("player:a")

    assert cache.get("a1") is None
    assert cache.get("a2") is None
    assert cache.get("b1") == 3
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_invalidate_player_keeps_other_player_cache() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        steam64 = request.url.params.get("game_player_id")
        payload = (
            PLAYER_TWO_PAYLOAD
            if steam64 == STEAM_ID64_TWO
            else PLAYER_PAYLOAD
        )
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        first = await client.get_player_by_steam_id(STEAM_ID64)
        second = await client.get_player_by_steam_id(STEAM_ID64_TWO)

        assert first is not None
        assert second is not None
        assert len(captured) == 2

        client.invalidate_player_by_steam_id(STEAM_ID64)
        await client.get_player_by_steam_id(STEAM_ID64)
        assert len(captured) == 3

        await client.get_player_by_steam_id(STEAM_ID64_TWO)
        assert len(captured) == 3


@pytest.mark.asyncio
async def test_single_flight_deduplicates_concurrent_requests() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json=LIFETIME_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        tasks = [
            asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
            for _ in range(5)
        ]
        await started.wait()
        for _ in range(10):
            await asyncio.sleep(0)

        assert calls == 1
        release.set()
        results = await asyncio.gather(*tasks)

        assert len(results) == 5
        assert all(
            result["player_id"] == PLAYER_ID for result in results
        )
        assert calls == 1
        assert client._inflight == {}


@pytest.mark.asyncio
async def test_single_flight_shares_errors_and_cleans_inflight() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(500)

    transport = AsyncMockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        transport=transport,
    ) as client:
        tasks = [
            asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
            for _ in range(3)
        ]
        await started.wait()
        for _ in range(10):
            await asyncio.sleep(0)

        assert calls == 1
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert len(results) == 3
        assert all(isinstance(result, FaceitError) for result in results)
        assert calls == 1
        assert client._inflight == {}

        with pytest.raises(FaceitError):
            await client.get_lifetime_stats(PLAYER_ID)

        assert calls == 2
        assert client._inflight == {}


@pytest.mark.asyncio
async def test_single_flight_keeps_request_after_owner_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json=LIFETIME_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        owner = asyncio.create_task(
            client.get_lifetime_stats(PLAYER_ID)
        )
        await started.wait()

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        # Реальный запрос продолжает жить под shield, запись in-flight
        # не удаляется до его завершения.
        assert len(client._inflight) == 1
        inner = next(iter(client._inflight.values()))

        release.set()
        await inner

        assert client._inflight == {}
        # Результат уже в кэше: повторный вызов не идёт в сеть.
        assert transport.requests and len(transport.requests) == 1
        stats = await client.get_lifetime_stats(PLAYER_ID)
        assert stats["player_id"] == PLAYER_ID
        assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_long_retry_after_is_not_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(
                429,
                headers={"Retry-After": "120"},
            ),
            captured=captured,
        )
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async with FaceitClient(
        "test-api-key",
        max_retries=3,
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit) as exc_info:
            await client.get_lifetime_stats(PLAYER_ID)

    assert exc_info.value.retry_after == pytest.approx(120.0)
    assert sleep_calls == []
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_global_backoff_blocks_network_after_429() -> None:
    now = [100.0]
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(429),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        backoff_base=0,
        rate_limit_cooldown=30.0,
        clock=lambda: now[0],
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit):
            await client.get_lifetime_stats(PLAYER_ID)

        assert len(captured) == 1

        with pytest.raises(FaceitRateLimit) as exc_info:
            await client.get_recent_matches(PLAYER_ID)

        assert len(captured) == 1
        assert exc_info.value.retry_after == pytest.approx(30.0)

        now[0] = 131.0
        with pytest.raises(FaceitRateLimit):
            await client.get_lifetime_stats(PLAYER_ID)

        assert len(captured) == 2


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_requests() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active >= 2:
            started.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return httpx.Response(200, json=RANKING_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        max_concurrency=2,
        transport=transport,
    ) as client:
        tasks = [
            asyncio.create_task(
                client.get_player_ranking(PLAYER_ID, region)
            )
            for region in ("EU", "NA", "SEA", "BR")
        ]

        await started.wait()
        for _ in range(10):
            await asyncio.sleep(0)

        assert active == 2
        assert max_active == 2

        release.set()
        await asyncio.gather(*tasks)

    assert max_active == 2

@pytest.mark.asyncio
async def test_invalidate_by_steam_clears_records_when_profile_expired() -> None:
    now = [1000.0]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path.endswith("/players"):
            return httpx.Response(200, json=PLAYER_PAYLOAD)
        if path.endswith("/stats/cs2"):
            return httpx.Response(200, json=LIFETIME_PAYLOAD)
        if path.endswith("/games/cs2/stats"):
            return httpx.Response(200, json=RECENT_MATCHES_PAYLOAD)
        return httpx.Response(200, json=RANKING_PAYLOAD)

    transport = httpx.MockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        clock=lambda: now[0],
        transport=transport,
    ) as client:
        await client.get_player_by_steam_id(STEAM_ID64)
        await client.get_lifetime_stats(PLAYER_ID, steam64=STEAM_ID64)
        await client.get_recent_matches(
            PLAYER_ID,
            steam64=STEAM_ID64,
        )
        await client.get_player_ranking(
            PLAYER_ID,
            "EU",
            steam64=STEAM_ID64,
        )

        profile_key = ("player_by_steam_id", STEAM_ID64)
        _, player = client._cache._items[profile_key]
        # Имитируем протухание только профиля: остальные записи живы,
        # карта steam64 -> player_id тоже ещё есть.
        client._cache._items[profile_key] = (now[0] - 1, player)
        assert client._cache.get(profile_key) is None

        client.invalidate_player_by_steam_id(STEAM_ID64)

        assert client._cache.get(("lifetime_stats", PLAYER_ID)) is None
        assert client._cache.get(
            ("recent_matches", PLAYER_ID, 20)
        ) is None
        assert client._cache.get(
            ("player_ranking", PLAYER_ID, "EU", None)
        ) is None


@pytest.mark.asyncio
async def test_invalidate_by_steam_clears_records_without_profile_or_map() -> None:
    now = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/players"):
            return httpx.Response(200, json=PLAYER_PAYLOAD)
        if path.endswith("/stats/cs2"):
            return httpx.Response(200, json=LIFETIME_PAYLOAD)
        if path.endswith("/games/cs2/stats"):
            return httpx.Response(200, json=RECENT_MATCHES_PAYLOAD)
        return httpx.Response(200, json=RANKING_PAYLOAD)

    transport = httpx.MockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        clock=lambda: now[0],
        transport=transport,
    ) as client:
        await client.get_player_by_steam_id(STEAM_ID64)
        await client.get_lifetime_stats(PLAYER_ID, steam64=STEAM_ID64)
        await client.get_recent_matches(
            PLAYER_ID,
            steam64=STEAM_ID64,
        )
        await client.get_player_ranking(
            PLAYER_ID,
            "EU",
            steam64=STEAM_ID64,
        )

        # Профиль вытеснен, карта соответствий потеряна. Инвалидация
        # всё равно обязана удалить записи по steam-тегу.
        client._cache._remove(("player_by_steam_id", STEAM_ID64))
        client._steam_to_player.clear()

        client.invalidate_player_by_steam_id(STEAM_ID64)

        assert client._cache.get(("lifetime_stats", PLAYER_ID)) is None
        assert client._cache.get(
            ("recent_matches", PLAYER_ID, 20)
        ) is None
        assert client._cache.get(
            ("player_ranking", PLAYER_ID, "EU", None)
        ) is None


@pytest.mark.asyncio
async def test_single_flight_waiter_survives_owner_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json=LIFETIME_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        owner = asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
        await started.wait()
        waiter = asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
        for _ in range(10):
            await asyncio.sleep(0)

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        release.set()
        result = await waiter

        assert result["player_id"] == PLAYER_ID
        assert calls == 1
        assert client._inflight == {}


@pytest.mark.asyncio
async def test_single_flight_caches_result_before_removing_inflight() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json=LIFETIME_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient("test-api-key", transport=transport) as client:
        request_key = client._request_key(
            f"/players/{PLAYER_ID}/stats/cs2",
            None,
        )
        cache = client._cache
        original_set = cache.set
        inflight_at_cache_set: list[bool] = []

        def tracking_set(
            key: Any,
            value: Any,
            *,
            tags: Any = (),
        ) -> None:
            inflight_at_cache_set.append(request_key in client._inflight)
            original_set(key, value, tags=tags)

        cache.set = tracking_set  # type: ignore[method-assign]

        task = asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
        await started.wait()
        release.set()
        await task

        assert inflight_at_cache_set == [True]
        assert client._inflight == {}

        await client.get_lifetime_stats(PLAYER_ID)
        assert calls == 1


@pytest.mark.asyncio
async def test_background_failure_after_owner_cancel_has_no_warning() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    unhandled: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def exception_handler(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        unhandled.append(context)

    loop.set_exception_handler(exception_handler)

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(500)

    transport = AsyncMockTransport(handler)

    try:
        async with FaceitClient(
            "test-api-key",
            max_retries=0,
            transport=transport,
        ) as client:
            owner = asyncio.create_task(
                client.get_lifetime_stats(PLAYER_ID)
            )
            await started.wait()
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner

            release.set()
            for _ in range(50):
                if not client._inflight:
                    break
                await asyncio.sleep(0)

            assert client._inflight == {}
            # Даём loop шанс сообщить о необработанном исключении.
            for _ in range(10):
                await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert unhandled == []


@pytest.mark.asyncio
async def test_retry_sleep_does_not_hold_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    real_sleep = asyncio.sleep
    lifetime_calls = 0
    recent_calls = 0

    async def fake_sleep(delay: float) -> None:
        sleep_started.set()
        await release_sleep.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lifetime_calls, recent_calls
        if request.url.path.endswith("/stats/cs2"):
            lifetime_calls += 1
            if lifetime_calls == 1:
                return httpx.Response(500)
            return httpx.Response(200, json=LIFETIME_PAYLOAD)
        recent_calls += 1
        return httpx.Response(200, json=RECENT_MATCHES_PAYLOAD)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = AsyncMockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        max_retries=1,
        backoff_base=0,
        rate_limit_cooldown=0.0,
        max_concurrency=1,
        transport=transport,
    ) as client:
        retrying = asyncio.create_task(
            client.get_lifetime_stats(PLAYER_ID)
        )
        await sleep_started.wait()

        other = asyncio.create_task(
            client.get_recent_matches(PLAYER_ID)
        )
        for _ in range(10):
            await real_sleep(0)

        # Пока лидер спит между попытками, слот свободен для другого запроса.
        assert recent_calls == 1

        release_sleep.set()
        await asyncio.gather(retrying, other)

    assert lifetime_calls == 2
    assert recent_calls == 1


@pytest.mark.asyncio
async def test_queued_request_rechecks_global_cooldown_after_semaphore() -> None:
    a_started = asyncio.Event()
    release_a = asyncio.Event()
    real_sleep = asyncio.sleep
    recent_calls = 0
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal recent_calls
        captured.append(request)
        if request.url.path.endswith("/stats/cs2"):
            a_started.set()
            await release_a.wait()
            return httpx.Response(429)
        recent_calls += 1
        return httpx.Response(200, json=RECENT_MATCHES_PAYLOAD)

    transport = AsyncMockTransport(handler)

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        rate_limit_cooldown=30.0,
        max_concurrency=1,
        transport=transport,
    ) as client:
        first = asyncio.create_task(client.get_lifetime_stats(PLAYER_ID))
        await a_started.wait()

        second = asyncio.create_task(
            client.get_recent_matches(PLAYER_ID)
        )
        for _ in range(10):
            await real_sleep(0)
        assert recent_calls == 0

        release_a.set()
        results = await asyncio.gather(
            first,
            second,
            return_exceptions=True,
        )

    assert all(isinstance(result, FaceitRateLimit) for result in results)
    assert recent_calls == 0
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_rate_limit_cooldown_uses_max_of_retry_after_and_base() -> None:
    now = [100.0]
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(
                429,
                headers={"Retry-After": "2"},
            ),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        rate_limit_cooldown=30.0,
        clock=lambda: now[0],
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit) as first_error:
            await client.get_lifetime_stats(PLAYER_ID)

        assert first_error.value.retry_after == pytest.approx(30.0)

        with pytest.raises(FaceitRateLimit) as second_error:
            await client.get_recent_matches(PLAYER_ID)

    assert second_error.value.retry_after == pytest.approx(30.0)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_rate_limit_cooldown_is_capped() -> None:
    now = [100.0]
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(
                429,
                headers={"Retry-After": "1000"},
            )
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        rate_limit_cooldown=30.0,
        rate_limit_cooldown_max=120.0,
        clock=lambda: now[0],
        transport=transport,
    ) as client:
        with pytest.raises(FaceitRateLimit) as exc_info:
            await client.get_lifetime_stats(PLAYER_ID)

    assert exc_info.value.retry_after == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_cached_response_is_served_during_rate_limit_cooldown() -> None:
    now = [100.0]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(200, json=LIFETIME_PAYLOAD)
        return httpx.Response(429)

    async with FaceitClient(
        "test-api-key",
        max_retries=0,
        rate_limit_cooldown=30.0,
        clock=lambda: now[0],
        transport=httpx.MockTransport(handler),
    ) as client:
        cached = await client.get_lifetime_stats(PLAYER_ID)
        assert cached["player_id"] == PLAYER_ID
        assert len(captured) == 1

        with pytest.raises(FaceitRateLimit):
            await client.get_recent_matches(PLAYER_ID)
        assert len(captured) == 2

        # Кэшированный ответ отдаётся даже при активном глобальном cooldown.
        again = await client.get_lifetime_stats(PLAYER_ID)
        assert again == cached
        assert len(captured) == 2


@pytest.mark.asyncio
async def test_player_404_is_negative_cached_and_invalidated_by_steam() -> None:
    now = [100.0]
    found = [False]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        steam64 = request.url.params.get("game_player_id")
        if steam64 == STEAM_ID64 and found[0]:
            return httpx.Response(200, json=PLAYER_PAYLOAD)
        return httpx.Response(404, json={"message": "Not found"})

    async with FaceitClient(
        "test-api-key",
        negative_cache_ttl=60.0,
        clock=lambda: now[0],
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.get_player_by_steam_id(STEAM_ID64) is None
        assert await client.get_player_by_steam_id(STEAM_ID64) is None
        assert len(captured) == 1

        assert await client.get_player_by_steam_id(STEAM_ID64_TWO) is None
        assert await client.get_player_by_steam_id(STEAM_ID64_TWO) is None
        assert len(captured) == 2

        # Инвалидация одного игрока не должна сбрасывать негативный кэш
        # другого и должна позволить refresh сходить в сеть заново.
        client.invalidate_player_by_steam_id(STEAM_ID64)
        found[0] = True

        player = await client.get_player_by_steam_id(STEAM_ID64)
        assert player is not None
        assert player["player_id"] == PLAYER_ID
        assert len(captured) == 3

        assert await client.get_player_by_steam_id(STEAM_ID64_TWO) is None
        assert len(captured) == 3


@pytest.mark.asyncio
async def test_player_negative_cache_expires() -> None:
    now = [100.0]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(404, json={"message": "Not found"})

    async with FaceitClient(
        "test-api-key",
        negative_cache_ttl=60.0,
        clock=lambda: now[0],
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.get_player_by_steam_id(STEAM_ID64) is None
        assert await client.get_player_by_steam_id(STEAM_ID64) is None
        assert len(captured) == 1

        now[0] = 161.0
        assert await client.get_player_by_steam_id(STEAM_ID64) is None
        assert len(captured) == 2
