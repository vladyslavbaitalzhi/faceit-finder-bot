from __future__ import annotations

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
)

STEAM_ID64 = "76561198000000000"
PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"

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
@pytest.mark.parametrize("retry_status", [429, 500, 503])
async def test_retries_on_429_and_5xx(retry_status: int) -> None:
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
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (429, FaceitRateLimit),
        (500, FaceitError),
    ],
)
async def test_retries_are_exhausted(
    status: int,
    error_type: type[FaceitError],
) -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        make_handler(
            response=httpx.Response(status),
            captured=captured,
        )
    )

    async with FaceitClient(
        "test-api-key",
        max_retries=2,
        backoff_base=0,
        transport=transport,
    ) as client:
        with pytest.raises(error_type):
            await client.get_player_by_steam_id(STEAM_ID64)

    assert len(captured) == 3


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
