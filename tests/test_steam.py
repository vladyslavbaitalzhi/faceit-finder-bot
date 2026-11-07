from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bot import steam
from bot.steam import (
    SteamIdentifier,
    fetch_steam_stats,
    parse_steam_input,
    resolve_steam_id,
)

STEAM_ID64 = "76561198000000000"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (STEAM_ID64, SteamIdentifier(kind="steamid64", value=STEAM_ID64)),
        (
            f"https://steamcommunity.com/profiles/{STEAM_ID64}",
            SteamIdentifier(kind="steamid64", value=STEAM_ID64),
        ),
        (
            f"http://steamcommunity.com/profiles/{STEAM_ID64}/",
            SteamIdentifier(kind="steamid64", value=STEAM_ID64),
        ),
        (
            f"https://www.steamcommunity.com/profiles/{STEAM_ID64}/",
            SteamIdentifier(kind="steamid64", value=STEAM_ID64),
        ),
        (
            f"https://steamcommunity.com/profiles/{STEAM_ID64}/?l=english",
            SteamIdentifier(kind="steamid64", value=STEAM_ID64),
        ),
        (
            "https://steamcommunity.com/id/vanity_name",
            SteamIdentifier(kind="vanity", value="vanity_name"),
        ),
        (
            "http://www.steamcommunity.com/id/Player-42/",
            SteamIdentifier(kind="vanity", value="Player-42"),
        ),
        ("vanity_name", SteamIdentifier(kind="vanity", value="vanity_name")),
        ("  Player_42  ", SteamIdentifier(kind="vanity", value="Player_42")),
    ],
)
def test_parse_steam_input_valid(raw: str, expected: SteamIdentifier) -> None:
    assert parse_steam_input(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "7656119800000000",  # 16 цифр
        "86561198000000000",  # 17 цифр, но неверный префикс
        "12345678901234567",  # 17 цифр, но не SteamID64
        "https://steamcommunity.com/profiles/7656119800000000",
        "https://steamcommunity.com/profiles/86561198000000000",
        "https://steamcommunity.com/profiles/12345678901234567",
        "https://steamcommunity.com/profiles/76561198000000000/extra",
        "https://steamcommunity.com/id/a",
        "https://steamcommunity.com/id/vanity.name",
        "https://steamcommunity.com/id/vanity_name/extra",
        "https://example.com/id/vanity_name",
        "steamcommunity.com/id/vanity_name",
        "not a vanity!",
    ],
)
def test_parse_steam_input_invalid(raw: str) -> None:
    assert parse_steam_input(raw) is None


@pytest.mark.asyncio
async def test_resolve_direct_steam_id_does_not_use_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_on_http(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Для прямого SteamID64 HTTP-запрос выполняться не должен")

    monkeypatch.setattr(steam.httpx, "AsyncClient", fail_on_http)

    result = await resolve_steam_id(
        f"https://steamcommunity.com/profiles/{STEAM_ID64}/"
    )

    assert result == STEAM_ID64


@pytest.mark.asyncio
async def test_resolve_invalid_input_does_not_use_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_on_http(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Для некорректного ввода HTTP-запрос выполняться не должен")

    monkeypatch.setattr(steam.httpx, "AsyncClient", fail_on_http)

    assert await resolve_steam_id("https://example.com/id/vanity_name") is None


class FakeResponse:
    """Минимальная замена httpx.Response для тестов."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        json_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", steam.STEAM_RESOLVE_VANITY_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
            )

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: FakeResponse | None = None,
    error: Exception | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Подменяет httpx.AsyncClient и возвращает список запросов."""
    calls: list[tuple[str, dict[str, Any] | None]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> FakeResponse:
            calls.append((url, params))
            if error is not None:
                raise error
            assert response is not None, "Тесту нужен response или error"
            return response

    monkeypatch.setattr(steam.httpx, "AsyncClient", FakeAsyncClient)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "vanity_name",
        "https://steamcommunity.com/id/vanity_name/",
    ],
)
async def test_resolve_vanity_success(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    calls = install_fake_client(
        monkeypatch,
        response=FakeResponse(
            {"response": {"success": 1, "steamid": STEAM_ID64}}
        ),
    )

    result = await resolve_steam_id(raw)

    assert result == STEAM_ID64
    assert len(calls) == 1
    url, params = calls[0]
    assert url == steam.STEAM_RESOLVE_VANITY_URL
    assert params == {
        "key": "test-api-key",
        "vanityurl": "vanity_name",
        "url_type": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"response": {"success": 42, "message": "No match"}},
        {"response": {}},
        {},
        {"response": {"success": 1, "steamid": "123"}},
        {"response": {"success": 1}},
        {"response": "invalid"},
        [],
    ],
)
async def test_resolve_vanity_not_found_or_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_client(monkeypatch, response=FakeResponse(payload))

    assert await resolve_steam_id("vanity_name") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.TimeoutException("timeout"),
        httpx.ConnectError("network error"),
    ],
)
async def test_resolve_vanity_network_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_client(monkeypatch, error=error)

    assert await resolve_steam_id("vanity_name") is None


@pytest.mark.asyncio
async def test_resolve_vanity_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_client(monkeypatch, response=FakeResponse(status_code=500))

    assert await resolve_steam_id("vanity_name") is None


@pytest.mark.asyncio
async def test_resolve_vanity_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_client(
        monkeypatch,
        response=FakeResponse(
            json_error=json.JSONDecodeError("invalid json", "", 0)
        ),
    )

    assert await resolve_steam_id("vanity_name") is None


@pytest.mark.asyncio
async def test_resolve_vanity_without_api_key_does_not_use_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: None)

    def fail_on_http(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Без STEAM_API_KEY HTTP-запрос выполняться не должен")

    monkeypatch.setattr(steam.httpx, "AsyncClient", fail_on_http)

    assert await resolve_steam_id("vanity_name") is None


def install_fake_steam_client(
    monkeypatch: pytest.MonkeyPatch,
    payloads: dict[str, Any],
    *,
    error: Exception | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Подменяет httpx.AsyncClient для запросов GetOwnedGames/GetPlayerBans."""
    calls: list[tuple[str, dict[str, Any] | None]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> FakeResponse:
            calls.append((url, params))
            if error is not None:
                raise error
            return FakeResponse(payloads.get(url))

    monkeypatch.setattr(steam.httpx, "AsyncClient", FakeAsyncClient)
    return calls


@pytest.mark.asyncio
async def test_fetch_steam_stats_without_api_key_does_not_use_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: None)

    def fail_on_http(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Без STEAM_API_KEY HTTP-запрос выполняться не должен")

    monkeypatch.setattr(steam.httpx, "AsyncClient", fail_on_http)

    assert await fetch_steam_stats(STEAM_ID64) is None


@pytest.mark.asyncio
async def test_fetch_steam_stats_parses_hours_and_vac_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    calls = install_fake_steam_client(
        monkeypatch,
        {
            steam.STEAM_OWNED_GAMES_URL: {
                "response": {
                    "games": [
                        {"appid": 570, "playtime_forever": 100},
                        {"appid": 730, "playtime_forever": 75000},
                    ]
                }
            },
            steam.STEAM_PLAYER_BANS_URL: {
                "players": [
                    {
                        "VACBanned": False,
                        "NumberOfVACBans": 0,
                        "DaysSinceLastBan": 0,
                    }
                ]
            },
        },
    )

    result = await fetch_steam_stats(STEAM_ID64)

    assert result is not None
    assert result.cs2_hours == pytest.approx(1250.0)
    assert result.vac_banned is False
    assert result.vac_bans == 0
    assert result.has_data is True

    assert {url for url, _ in calls} == {
        steam.STEAM_OWNED_GAMES_URL,
        steam.STEAM_PLAYER_BANS_URL,
    }
    params_by_url = dict(calls)
    assert params_by_url[steam.STEAM_OWNED_GAMES_URL] == {
        "key": "test-api-key",
        "steamid": STEAM_ID64,
        "include_appinfo": 0,
        "include_played_free_games": 1,
    }
    assert params_by_url[steam.STEAM_PLAYER_BANS_URL] == {
        "key": "test-api-key",
        "steamids": STEAM_ID64,
    }


@pytest.mark.asyncio
async def test_fetch_steam_stats_parses_active_vac_ban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_steam_client(
        monkeypatch,
        {
            steam.STEAM_OWNED_GAMES_URL: {
                "response": {
                    "games": [{"appid": 730, "playtime_forever": 60}]
                }
            },
            steam.STEAM_PLAYER_BANS_URL: {
                "players": [
                    {
                        "VACBanned": True,
                        "NumberOfVACBans": 2,
                        "DaysSinceLastBan": 30,
                    }
                ]
            },
        },
    )

    result = await fetch_steam_stats(STEAM_ID64)

    assert result is not None
    assert result.cs2_hours == pytest.approx(1.0)
    assert result.vac_banned is True
    assert result.vac_bans == 2
    assert result.days_since_last_ban == 30


@pytest.mark.asyncio
async def test_fetch_steam_stats_returns_zero_hours_without_cs2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_steam_client(
        monkeypatch,
        {
            steam.STEAM_OWNED_GAMES_URL: {
                "response": {"games": [{"appid": 570}]}
            },
            steam.STEAM_PLAYER_BANS_URL: {
                "players": [{"VACBanned": False, "NumberOfVACBans": 0}]
            },
        },
    )

    result = await fetch_steam_stats(STEAM_ID64)

    assert result is not None
    assert result.cs2_hours == 0.0


@pytest.mark.asyncio
async def test_fetch_steam_stats_returns_none_for_private_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_steam_client(
        monkeypatch,
        {
            steam.STEAM_OWNED_GAMES_URL: {"response": {}},
            steam.STEAM_PLAYER_BANS_URL: {"players": []},
        },
    )

    assert await fetch_steam_stats(STEAM_ID64) is None


@pytest.mark.asyncio
async def test_fetch_steam_stats_returns_none_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steam, "_get_api_key", lambda: "test-api-key")
    install_fake_steam_client(
        monkeypatch,
        {},
        error=httpx.ConnectError("network error"),
    )

    assert await fetch_steam_stats(STEAM_ID64) is None
