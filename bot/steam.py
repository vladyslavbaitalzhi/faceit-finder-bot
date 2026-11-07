from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

logger = logging.getLogger(__name__)

STEAM_RESOLVE_VANITY_URL: Final[str] = (
    "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
)
STEAM_OWNED_GAMES_URL: Final[str] = (
    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
)
STEAM_PLAYER_BANS_URL: Final[str] = (
    "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
)
STEAM_REQUEST_TIMEOUT: Final[float] = 10.0
CS2_APP_ID: Final[int] = 730

# SteamID64 индивидуального аккаунта: 17 цифр, начинается с 7656119.
STEAM_ID64_RE: Final[re.Pattern[str]] = re.compile(r"^7656119\d{10}$")
VANITY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{2,32}$")

STEAM_PROFILE_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?steamcommunity\.com/profiles/"
    r"(?P<steamid64>7656119\d{10})/?(?:[?#]\S*)?$",
    re.IGNORECASE,
)
STEAM_VANITY_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?steamcommunity\.com/id/"
    r"(?P<vanity>[A-Za-z0-9_-]{2,32})/?(?:[?#]\S*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SteamIdentifier:
    """Результат разбора пользовательского ввода."""

    kind: Literal["steamid64", "vanity"]
    value: str


@dataclass(frozen=True, slots=True)
class SteamStats:
    """Дополнительные данные Steam-профиля."""

    cs2_hours: float | None = None
    vac_banned: bool | None = None
    vac_bans: int | None = None
    days_since_last_ban: int | None = None

    @property
    def has_data(self) -> bool:
        """Есть ли хотя бы один показатель для вывода."""
        return self.cs2_hours is not None or self.vac_banned is not None


def parse_steam_input(raw: str) -> SteamIdentifier | None:
    """Разбирает ссылку на Steam-профиль, SteamID64 или vanity-имя.

    Поддерживаются:
    - ``https://steamcommunity.com/profiles/<SteamID64>`` (http/https, www,
      завершающий слэш, query-параметры и якорь);
    - ``https://steamcommunity.com/id/<vanity>``;
    - голый SteamID64 из 17 цифр, начинающийся с ``7656119``;
    - голое vanity-имя.

    Args:
        raw: Ссылка или идентификатор Steam-профиля.

    Returns:
        ``SteamIdentifier`` с типом и значением либо ``None``, если ввод
        не соответствует поддерживаемым форматам.
    """
    value = raw.strip()
    if not value:
        return None

    match = STEAM_PROFILE_URL_RE.fullmatch(value)
    if match is not None:
        return SteamIdentifier(kind="steamid64", value=match.group("steamid64"))

    match = STEAM_VANITY_URL_RE.fullmatch(value)
    if match is not None:
        return SteamIdentifier(kind="vanity", value=match.group("vanity"))

    if STEAM_ID64_RE.fullmatch(value):
        return SteamIdentifier(kind="steamid64", value=value)

    # Числовая строка, не являющаяся SteamID64, — почти наверняка опечатка,
    # а не vanity-имя. Не отправляем её в Steam API.
    if value.isdigit():
        return None

    if VANITY_RE.fullmatch(value):
        return SteamIdentifier(kind="vanity", value=value)

    return None


def _get_api_key() -> str | None:
    """Возвращает Steam Web API key из настроек приложения.

    Импорт ``config`` выполняется лениво, чтобы импорт модуля и разбор ссылок
    не зависели от загрузки настроек бота.
    """
    from config import settings

    if settings.steam_api_key is None:
        return None

    api_key = settings.steam_api_key.get_secret_value().strip()
    return api_key or None


async def _resolve_vanity(vanity: str, api_key: str) -> str | None:
    """Разрешает vanity-имя в SteamID64 через Steam Web API.

    Args:
        vanity: Короткое имя из ссылки ``/id/<vanity>``.
        api_key: Ключ Steam Web API.

    Returns:
        SteamID64 при успешном ответе API, иначе ``None``.
    """
    params: dict[str, str | int] = {
        "key": api_key,
        "vanityurl": vanity,
        "url_type": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=STEAM_REQUEST_TIMEOUT) as client:
            response = await client.get(STEAM_RESOLVE_VANITY_URL, params=params)
            response.raise_for_status()
            payload: Any = response.json()
    except httpx.TimeoutException:
        logger.warning(
            "Steam API не ответил вовремя при разрешении vanity-имени %r",
            vanity,
        )
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Steam API вернул HTTP %s при разрешении vanity-имени %r",
            exc.response.status_code,
            vanity,
        )
        return None
    except httpx.RequestError as exc:
        logger.warning(
            "Сетевая ошибка при обращении к Steam API для %r: %s",
            vanity,
            exc,
        )
        return None
    except ValueError:
        logger.warning(
            "Steam API вернул некорректный JSON для vanity-имени %r",
            vanity,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning("Steam API вернул неожиданный формат ответа для %r", vanity)
        return None

    data = payload.get("response")
    if not isinstance(data, dict):
        logger.warning("В ответе Steam API нет объекта response для %r", vanity)
        return None

    if data.get("success") != 1:
        logger.info(
            "Steam не нашёл профиль для vanity-имени %r (success=%r)",
            vanity,
            data.get("success"),
        )
        return None

    steam_id = data.get("steamid")
    if not isinstance(steam_id, str) or not STEAM_ID64_RE.fullmatch(steam_id):
        logger.warning("Steam API вернул некорректный SteamID64 для %r", vanity)
        return None

    return steam_id


async def resolve_steam_id(url: str) -> str | None:
    """Возвращает SteamID64 по ссылке, SteamID64 или vanity-имени.

    Для прямых ссылок ``/profiles/...`` и голого SteamID64 сетевой запрос не
    выполняется. Для vanity-имён используется
    ``ISteamUser/ResolveVanityURL``.

    Args:
        url: Ссылка на Steam-профиль, SteamID64 или vanity-имя.

    Returns:
        SteamID64 либо ``None``, если ввод некорректен, профиль не найден,
        не задан ``STEAM_API_KEY`` или Steam API недоступен.
    """
    identifier = parse_steam_input(url)
    if identifier is None:
        logger.warning("Некорректная ссылка или SteamID64: %r", url)
        return None

    if identifier.kind == "steamid64":
        return identifier.value

    api_key = _get_api_key()
    if api_key is None:
        logger.warning(
            "STEAM_API_KEY не задан — невозможно разрешить vanity-имя %r",
            identifier.value,
        )
        return None

    return await _resolve_vanity(identifier.value, api_key)


def _to_int(value: Any) -> int | None:
    """Безопасно приводит значение Steam API к int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(normalized)
        except ValueError:
            return None
    return None


async def _steam_payload(
    client: httpx.AsyncClient,
    url: str,
    params: Mapping[str, str | int],
    label: str,
) -> dict[str, Any] | None:
    """Загружает JSON-ответ Steam Web API, не роняя основной сценарий."""
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload: Any = response.json()
    except httpx.TimeoutException:
        logger.warning("Steam API не ответил вовремя: %s", label)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Steam API вернул HTTP %s: %s",
            exc.response.status_code,
            label,
        )
        return None
    except httpx.RequestError as exc:
        logger.warning("Сетевая ошибка Steam API (%s): %s", label, exc)
        return None
    except ValueError:
        logger.warning("Steam API вернул некорректный JSON: %s", label)
        return None

    if not isinstance(payload, dict):
        logger.warning("Steam API вернул неожиданный формат: %s", label)
        return None

    return payload


def _parse_cs2_hours(payload: Mapping[str, Any] | None) -> float | None:
    """Извлекает часы в CS2 из ответа GetOwnedGames."""
    if not isinstance(payload, Mapping):
        return None

    response = payload.get("response")
    if not isinstance(response, Mapping):
        return None

    games = response.get("games")
    if not isinstance(games, list) or not games:
        return None

    for game in games:
        if not isinstance(game, Mapping):
            continue
        app_id = _to_int(game.get("appid"))
        if app_id != CS2_APP_ID:
            continue

        minutes = _to_int(game.get("playtime_forever"))
        if minutes is None:
            return None
        return max(0.0, minutes / 60)

    # Список игр получен, но CS2 в нём нет — значит, наиграно 0 часов.
    return 0.0


def _parse_vac_bans(
    payload: Mapping[str, Any] | None,
) -> tuple[bool | None, int | None, int | None]:
    """Извлекает информацию о VAC-бане из ответа GetPlayerBans."""
    if not isinstance(payload, Mapping):
        return None, None, None

    players = payload.get("players")
    if not isinstance(players, list) or not players:
        return None, None, None

    player = players[0]
    if not isinstance(player, Mapping):
        return None, None, None

    raw_banned = player.get("VACBanned")
    vac_banned = raw_banned if isinstance(raw_banned, bool) else None
    vac_bans = _to_int(player.get("NumberOfVACBans"))
    days_since_last_ban = _to_int(player.get("DaysSinceLastBan"))

    return vac_banned, vac_bans, days_since_last_ban


async def fetch_steam_stats(steam64: str) -> SteamStats | None:
    """Загружает часы в CS2 и информацию о VAC-бане.

    Args:
        steam64: SteamID64 игрока.

    Returns:
        ``SteamStats`` либо ``None``, если ключ не задан, профиль закрыт
        или Steam API недоступен. Ошибки не пробрасываются.
    """
    api_key = _get_api_key()
    if api_key is None:
        return None

    owned_params: dict[str, str | int] = {
        "key": api_key,
        "steamid": steam64,
        "include_appinfo": 0,
        "include_played_free_games": 1,
    }
    bans_params: dict[str, str | int] = {
        "key": api_key,
        "steamids": steam64,
    }

    try:
        async with httpx.AsyncClient(timeout=STEAM_REQUEST_TIMEOUT) as client:
            owned_payload, bans_payload = await asyncio.gather(
                _steam_payload(
                    client,
                    STEAM_OWNED_GAMES_URL,
                    owned_params,
                    "GetOwnedGames",
                ),
                _steam_payload(
                    client,
                    STEAM_PLAYER_BANS_URL,
                    bans_params,
                    "GetPlayerBans",
                ),
            )
    except httpx.RequestError as exc:
        logger.warning("Не удалось создать запрос к Steam API: %s", exc)
        return None

    cs2_hours = _parse_cs2_hours(owned_payload)
    vac_banned, vac_bans, days_since_last_ban = _parse_vac_bans(
        bans_payload
    )

    if cs2_hours is None and vac_banned is None:
        return None

    return SteamStats(
        cs2_hours=cs2_hours,
        vac_banned=vac_banned,
        vac_bans=vac_bans,
        days_since_last_ban=days_since_last_ban,
    )
