from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, Generic, Self, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

logger = logging.getLogger(__name__)

FACEIT_API_BASE_URL: Final[str] = "https://open.faceit.com/data/v4"
CACHE_TTL_SECONDS: Final[float] = 300.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_SECONDS: Final[float] = 0.5


class FaceitError(Exception):
    """Базовая ошибка при работе с Faceit Data API."""


class FaceitNotFound(FaceitError):
    """Запрошенный ресурс Faceit не найден (HTTP 404)."""


class FaceitRateLimit(FaceitError):
    """Faceit Data API ограничил частоту запросов (HTTP 429)."""

    def __init__(
        self,
        message: str = "Faceit API: превышен лимит запросов",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FaceitGameProfile(BaseModel):
    """Профиль игрока в конкретной игре Faceit."""

    model_config = ConfigDict(extra="ignore")

    region: str | None = None
    game_player_id: str | None = None
    game_player_name: str | None = None
    skill_level: int | None = None
    skill_level_label: str | None = None
    faceit_elo: int | None = None


class FaceitPlayer(BaseModel):
    """Ответ Faceit API с данными игрока."""

    model_config = ConfigDict(extra="ignore")

    player_id: str
    nickname: str
    country: str | None = None
    avatar: str | None = None
    faceit_url: str | None = None
    games: dict[str, FaceitGameProfile | None] = Field(default_factory=dict)

    @computed_field
    @property
    def region(self) -> str | None:
        """Регион игрока в CS2."""
        cs2 = self.games.get("cs2")
        return cs2.region if cs2 is not None else None


class FaceitStatsSegment(BaseModel):
    """Сегмент статистики Faceit (например, карта)."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    label: str | None = None
    mode: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


class FaceitLifetimeStats(BaseModel):
    """Lifetime-статистика игрока CS2."""

    model_config = ConfigDict(extra="ignore")

    player_id: str | None = None
    game_id: str | None = None
    lifetime: dict[str, Any] = Field(default_factory=dict)
    segments: list[FaceitStatsSegment] = Field(default_factory=list)


class FaceitMatch(BaseModel):
    """Один недавний матч игрока."""

    model_config = ConfigDict(extra="ignore")

    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: int | None = None


class FaceitRecentMatches(BaseModel):
    """Ответ Faceit API с недавними матчами игрока."""

    model_config = ConfigDict(extra="ignore")

    player_id: str | None = None
    game_id: str | None = None
    items: list[FaceitMatch] = Field(default_factory=list)


class FaceitPlayerRanking(BaseModel):
    """Ответ Faceit API с позицией игрока в рейтинге."""

    model_config = ConfigDict(extra="ignore")

    position: int | None = None
    start: int | None = None
    end: int | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)


class FaceitMatchDetails(BaseModel):
    """Детали матча Faceit (счёт и составы)."""

    model_config = ConfigDict(extra="ignore")

    match_id: str | None = None
    results: dict[str, Any] = Field(default_factory=dict)
    teams: dict[str, Any] = Field(default_factory=dict)


CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")
ModelT = TypeVar("ModelT", bound=BaseModel)


class _TTLCache(Generic[CacheKey, CacheValue]):
    """Минималистичный in-memory TTL-кэш."""

    def __init__(
        self,
        ttl: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl < 0:
            raise ValueError("TTL не может быть отрицательным")

        self._ttl = ttl
        self._clock = clock
        self._items: dict[CacheKey, tuple[float, CacheValue]] = {}

    def get(self, key: CacheKey) -> CacheValue | None:
        """Возвращает значение, если срок его жизни ещё не истёк."""
        item = self._items.get(key)
        if item is None:
            return None

        expires_at, value = item
        if self._clock() >= expires_at:
            self._items.pop(key, None)
            return None

        return value

    def set(self, key: CacheKey, value: CacheValue) -> None:
        """Сохраняет значение на время TTL."""
        if self._ttl == 0:
            self._items.pop(key, None)
            return

        self._items[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        """Полностью очищает кэш."""
        self._items.clear()


class FaceitClient:
    """Асинхронный клиент Faceit Data API v4.

    Один экземпляр клиента переиспользует общий ``httpx.AsyncClient``.
    Для корректного закрытия соединений используйте async context manager
    или вызывайте :meth:`aclose`.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_SECONDS,
        cache_ttl: float = CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries не может быть отрицательным")
        if backoff_base < 0:
            raise ValueError("backoff_base не может быть отрицательным")

        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._cache: _TTLCache[tuple[Any, ...], BaseModel] = _TTLCache(cache_ttl)
        client_kwargs: dict[str, Any] = {
            "base_url": FACEIT_API_BASE_URL,
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            "timeout": timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрывает общий HTTP-клиент."""
        await self._client.aclose()

    def clear_cache(self) -> None:
        """Очищает кэш ответов Faceit API."""
        self._cache.clear()

    def _backoff_delay(self, attempt: int) -> float:
        """Возвращает экспоненциальную задержку перед повтором."""
        return self._backoff_base * (2**attempt)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Читает Retry-After из ответа, если он задан в секундах."""
        raw_value = response.headers.get("Retry-After")
        if raw_value is None:
            return None

        try:
            return max(0.0, float(raw_value))
        except ValueError:
            return None

    async def _request(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        """Выполняет GET-запрос с ретраями на 429 и 5xx."""
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise FaceitError("Faceit API: истёк таймаут запроса") from exc
            except httpx.RequestError as exc:
                raise FaceitError("Faceit API: сетевая ошибка") from exc

            if response.status_code == 404:
                raise FaceitNotFound(f"Faceit API: ресурс не найден ({path})")

            if response.status_code == 429:
                retry_after = self._retry_after(response)
                if attempt >= self._max_retries:
                    raise FaceitRateLimit(retry_after=retry_after)

                delay = (
                    retry_after
                    if retry_after is not None
                    else self._backoff_delay(attempt)
                )
                logger.warning(
                    "Faceit API вернул 429, повтор через %.2f с (попытка %d/%d)",
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                if attempt >= self._max_retries:
                    raise FaceitError(
                        "Faceit API вернул HTTP "
                        f"{response.status_code} после "
                        f"{self._max_retries + 1} попыток"
                    )

                delay = self._backoff_delay(attempt)
                logger.warning(
                    "Faceit API вернул HTTP %d, повтор через %.2f с "
                    "(попытка %d/%d)",
                    response.status_code,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)
                continue

            if response.is_error:
                raise FaceitError(
                    f"Faceit API вернул HTTP {response.status_code}"
                )

            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise FaceitError("Faceit API вернул некорректный JSON") from exc

            if not isinstance(payload, dict):
                raise FaceitError("Faceit API вернул неожиданный формат ответа")

            return payload

        raise FaceitError("Faceit API: не удалось выполнить запрос")

    @staticmethod
    def _validate(model: type[ModelT], payload: dict[str, Any]) -> ModelT:
        """Валидирует ответ Faceit API через Pydantic-модель."""
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise FaceitError("Faceit API вернул ответ неожиданной структуры") from exc

    async def get_player_by_steam_id(
        self,
        steam64: str,
    ) -> dict[str, Any] | None:
        """Находит игрока Faceit по SteamID64.

        Args:
            steam64: SteamID64 игрока.

        Returns:
            Словарь с данными игрока либо ``None``, если игрок не найден.

        Raises:
            FaceitRateLimit: Если лимит запросов исчерпан после ретраев.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("player_by_steam_id", steam64)
        cached = self._cache.get(cache_key)
        if isinstance(cached, FaceitPlayer):
            return cached.model_dump()

        try:
            payload = await self._request(
                "/players",
                params={"game": "cs2", "game_player_id": steam64},
            )
        except FaceitNotFound:
            return None

        player = self._validate(FaceitPlayer, payload)
        self._cache.set(cache_key, player)
        return player.model_dump()

    async def get_lifetime_stats(self, player_id: str) -> dict[str, Any]:
        """Получает lifetime-статистику игрока и сегменты по картам.

        Args:
            player_id: Faceit player_id.

        Returns:
            Словарь с полями ``lifetime`` и ``segments``.

        Raises:
            FaceitNotFound: Если игрок или статистика не найдены.
            FaceitRateLimit: Если лимит запросов исчерпан после ретраев.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("lifetime_stats", player_id)
        cached = self._cache.get(cache_key)
        if isinstance(cached, FaceitLifetimeStats):
            return cached.model_dump()

        encoded_player_id = quote(player_id, safe="")
        payload = await self._request(f"/players/{encoded_player_id}/stats/cs2")
        stats = self._validate(FaceitLifetimeStats, payload)
        self._cache.set(cache_key, stats)
        return stats.model_dump()

    async def get_recent_matches(
        self,
        player_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Получает последние матчи игрока в CS2.

        Args:
            player_id: Faceit player_id.
            limit: Максимальное количество матчей (по умолчанию 20).

        Returns:
            Словарь с ключом ``items`` — списком матчей.

        Raises:
            ValueError: Если ``limit`` меньше 1.
            FaceitNotFound: Если игрок не найден.
            FaceitRateLimit: Если лимит запросов исчерпан после ретраев.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        if limit < 1:
            raise ValueError("limit должен быть больше 0")

        cache_key = ("recent_matches", player_id, limit)
        cached = self._cache.get(cache_key)
        if isinstance(cached, FaceitRecentMatches):
            return cached.model_dump()

        encoded_player_id = quote(player_id, safe="")
        payload = await self._request(
            f"/players/{encoded_player_id}/games/cs2/stats",
            params={"limit": limit},
        )
        matches = self._validate(FaceitRecentMatches, payload)
        self._cache.set(cache_key, matches)
        return matches.model_dump()

    async def get_player_ranking(
        self,
        player_id: str,
        region: str,
        *,
        country: str | None = None,
    ) -> dict[str, Any] | None:
        """Получает позицию игрока в рейтинге региона или страны.

        Args:
            player_id: Faceit player_id.
            region: Регион игрока (например, ``EU``).
            country: ISO-код страны для рейтинга страны.

        Returns:
            Словарь с полем ``position`` либо ``None``, если игрок
            не найден в рейтинге.

        Raises:
            FaceitRateLimit: Если лимит запросов исчерпан после ретраев.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("player_ranking", player_id, region, country)
        cached = self._cache.get(cache_key)
        if isinstance(cached, FaceitPlayerRanking):
            return cached.model_dump()

        encoded_player_id = quote(player_id, safe="")
        encoded_region = quote(region, safe="")
        params: dict[str, str | int] = {}
        if country:
            params["country"] = country

        try:
            payload = await self._request(
                f"/rankings/games/cs2/regions/{encoded_region}"
                f"/players/{encoded_player_id}",
                params=params or None,
            )
        except FaceitNotFound:
            return None

        ranking = self._validate(FaceitPlayerRanking, payload)
        self._cache.set(cache_key, ranking)
        return ranking.model_dump()

    async def get_match_details(
        self,
        match_id: str,
    ) -> dict[str, Any] | None:
        """Получает детали матча, включая счёт.

        Args:
            match_id: Идентификатор матча Faceit.

        Returns:
            Словарь с блоками ``results`` и ``teams`` либо ``None``,
            если матч не найден.

        Raises:
            FaceitRateLimit: Если лимит запросов исчерпан после ретраев.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("match_details", match_id)
        cached = self._cache.get(cache_key)
        if isinstance(cached, FaceitMatchDetails):
            return cached.model_dump()

        encoded_match_id = quote(match_id, safe="")
        try:
            payload = await self._request(f"/matches/{encoded_match_id}")
        except FaceitNotFound:
            return None

        details = self._validate(FaceitMatchDetails, payload)
        self._cache.set(cache_key, details)
        return details.model_dump()
