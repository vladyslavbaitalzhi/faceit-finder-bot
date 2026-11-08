from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
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
DEFAULT_MAX_CONCURRENCY: Final[int] = 5
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS: Final[float] = 30.0
DEFAULT_RATE_LIMIT_COOLDOWN_MAX_SECONDS: Final[float] = 300.0
DEFAULT_CACHE_MAX_ENTRIES: Final[int] = 2500
DEFAULT_CACHE_PURGE_INTERVAL: Final[int] = 100
DEFAULT_MAX_REQUESTS_PER_SECOND: Final[float] = 4.0
DEFAULT_NEGATIVE_CACHE_TTL_SECONDS: Final[float] = 60.0


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


class _TokenBucket:
    """Асинхронный token bucket для ограничения исходящих запросов."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: float | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError(
                "rate_per_second должен быть больше 0"
            )

        self._rate = rate_per_second
        # Всплеск не больше секунды: консервативно для внешнего API.
        self._capacity = max(
            1.0,
            rate_per_second if capacity is None else capacity,
        )
        self._tokens = self._capacity
        self._updated_at = clock()
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        """Текущее число токенов (для диагностики и тестов)."""
        return self._tokens

    async def acquire(self) -> float:
        """Забирает токен, при необходимости дожидаясь пополнения.

        Возвращает суммарное время ожидания в секундах. Состояние
        изменяется под ``asyncio.Lock``, поэтому параллельные запросы
        не могут одновременно забрать один и тот же токен.
        """
        total_wait = 0.0
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._rate,
                )
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return total_wait

                wait = (1.0 - self._tokens) / self._rate

            total_wait += wait
            await asyncio.sleep(wait)


class _TTLCache(Generic[CacheKey, CacheValue]):
    """Минималистичный in-memory TTL/LRU-кэш с тегами и лимитом размера."""

    def __init__(
        self,
        ttl: float,
        *,
        max_entries: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        purge_interval: int = DEFAULT_CACHE_PURGE_INTERVAL,
    ) -> None:
        if ttl < 0:
            raise ValueError("TTL не может быть отрицательным")
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries должен быть больше 0")
        if purge_interval < 1:
            raise ValueError("purge_interval должен быть больше 0")

        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._purge_interval = purge_interval
        self._operations = 0
        self._items: OrderedDict[CacheKey, tuple[float, CacheValue]] = (
            OrderedDict()
        )
        self._key_tags: dict[CacheKey, set[str]] = {}
        self._tag_keys: dict[str, set[CacheKey]] = {}

    def get(self, key: CacheKey) -> CacheValue | None:
        """Возвращает значение, если срок его жизни ещё не истёк."""
        item = self._items.get(key)
        if item is None:
            return None

        expires_at, value = item
        if self._clock() >= expires_at:
            self._remove(key)
            return None

        # dict сохраняет порядок вставки: перенос в конец делает его LRU.
        self._items.move_to_end(key)
        return value

    def peek(self, key: CacheKey) -> CacheValue | None:
        """Возвращает живое значение без запроса и изменения LRU-порядка."""
        item = self._items.get(key)
        if item is None:
            return None

        expires_at, value = item
        if self._clock() >= expires_at:
            return None
        return value

    def set(
        self,
        key: CacheKey,
        value: CacheValue,
        *,
        tags: Iterable[str] = (),
    ) -> None:
        """Сохраняет значение на время TTL и привязывает его к тегам."""
        if self._ttl == 0:
            self._remove(key)
            return

        now = self._clock()
        self._remove(key)

        self._operations += 1
        if self._operations % self._purge_interval == 0:
            self._purge_expired(now)

        if self._max_entries is not None:
            if len(self._items) >= self._max_entries:
                # Перед вытеснением освобождаем место от протухших записей,
                # чтобы они не вытесняли живые значения.
                self._purge_expired(now)
            while len(self._items) >= self._max_entries:
                oldest_key, _ = self._items.popitem(last=False)
                self._remove_tags(oldest_key)

        self._items[key] = (now + self._ttl, value)

        tag_set = {tag for tag in tags if tag}
        if tag_set:
            self._key_tags[key] = tag_set
            for tag in tag_set:
                self._tag_keys.setdefault(tag, set()).add(key)

    def invalidate_tag(self, tag: str) -> None:
        """Удаляет все записи, привязанные к тегу."""
        keys = self._tag_keys.pop(tag, set())
        for key in list(keys):
            self._remove(key)

    def clear(self) -> None:
        """Полностью очищает кэш."""
        self._items.clear()
        self._key_tags.clear()
        self._tag_keys.clear()

    def _purge_expired(self, now: float) -> None:
        for key, (expires_at, _) in list(self._items.items()):
            if now >= expires_at:
                self._remove(key)

    def _remove(self, key: CacheKey) -> None:
        self._items.pop(key, None)
        self._remove_tags(key)

    def _remove_tags(self, key: CacheKey) -> None:
        tags = self._key_tags.pop(key, None)
        if not tags:
            return

        for tag in tags:
            keys = self._tag_keys.get(tag)
            if keys is not None:
                keys.discard(key)
                if not keys:
                    self._tag_keys.pop(tag, None)

    def __len__(self) -> int:
        """Возвращает текущее число записей."""
        return len(self._items)


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
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        rate_limit_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        rate_limit_cooldown_max: float = (
            DEFAULT_RATE_LIMIT_COOLDOWN_MAX_SECONDS
        ),
        max_requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
        negative_cache_ttl: float = DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries не может быть отрицательным")
        if backoff_base < 0:
            raise ValueError("backoff_base не может быть отрицательным")
        if max_concurrency < 1:
            raise ValueError("max_concurrency должен быть больше 0")
        if rate_limit_cooldown < 0:
            raise ValueError(
                "rate_limit_cooldown не может быть отрицательным"
            )
        if rate_limit_cooldown_max < 0:
            raise ValueError(
                "rate_limit_cooldown_max не может быть отрицательным"
            )
        if max_requests_per_second <= 0:
            raise ValueError(
                "max_requests_per_second должен быть больше 0"
            )
        if negative_cache_ttl < 0:
            raise ValueError(
                "negative_cache_ttl не может быть отрицательным"
            )

        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._rate_limit_cooldown = rate_limit_cooldown
        self._rate_limit_cooldown_max = rate_limit_cooldown_max
        self._clock = clock
        self._cache_max_entries = cache_max_entries
        self._cache: _TTLCache[tuple[Any, ...], BaseModel] = _TTLCache(
            cache_ttl,
            max_entries=cache_max_entries,
            clock=clock,
        )
        self._negative_cache: _TTLCache[tuple[Any, ...], bool] = _TTLCache(
            negative_cache_ttl,
            max_entries=cache_max_entries,
            clock=clock,
        )
        self._token_bucket = _TokenBucket(
            max_requests_per_second,
            clock=clock,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._inflight: dict[
            tuple[str, tuple[tuple[str, str | int], ...]],
            asyncio.Task[Any],
        ] = {}
        self._rate_limited_until = 0.0
        self._steam_to_player: dict[str, str] = {}
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
        """Отменяет незавершённые single-flight задачи и закрывает HTTP-клиент."""
        pending = [
            task for task in self._inflight.values() if not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        await self._client.aclose()

    def clear_cache(self) -> None:
        """Полностью очищает кэш ответов Faceit API.

        Метод оставлен для служебных сценариев. Пользовательские кнопки
        «Обновить» используют точечную инвалидацию конкретного игрока.
        """
        self._cache.clear()
        self._negative_cache.clear()
        self._steam_to_player.clear()

    def invalidate_player(self, player_id: str) -> None:
        """Удаляет из кэша все записи указанного Faceit-игрока."""
        self._cache.invalidate_tag(f"player:{player_id}")

    def invalidate_player_by_steam_id(self, steam64: str) -> None:
        """Удаляет кэш игрока, найденного по SteamID64."""
        cached = self._cache.get(("player_by_steam_id", steam64))
        player_id = self._steam_to_player.get(steam64)
        if player_id is None and isinstance(cached, FaceitPlayer):
            player_id = cached.player_id

        self._cache.invalidate_tag(f"steam:{steam64}")
        self._negative_cache.invalidate_tag(f"steam:{steam64}")
        if player_id is not None:
            self._cache.invalidate_tag(f"player:{player_id}")
        self._steam_to_player.pop(steam64, None)

    @staticmethod
    def _player_tags(
        player_id: str,
        steam64: str | None,
    ) -> tuple[str, ...]:
        """Теги записи игрока: по Faceit player_id и, если известен, Steam."""
        tags = [f"player:{player_id}"]
        if steam64:
            tags.append(f"steam:{steam64}")
        return tuple(tags)

    def _remember_player(self, steam64: str, player_id: str) -> None:
        """Запоминает соответствие SteamID64 → player_id для инвалидации."""
        self._steam_to_player[steam64] = player_id
        while len(self._steam_to_player) > self._cache_max_entries:
            oldest_steam = next(iter(self._steam_to_player))
            self._steam_to_player.pop(oldest_steam)

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

    @staticmethod
    def _request_key(
        path: str,
        params: Mapping[str, str | int] | None,
    ) -> tuple[str, tuple[tuple[str, str | int], ...]]:
        """Строит ключ single-flight по эндпоинту и параметрам."""
        normalized = tuple(
            sorted(
                (str(name), value)
                for name, value in (params or {}).items()
            )
        )
        return path, normalized

    def _finish_inflight(
        self,
        key: tuple[str, tuple[tuple[str, str | int], ...]],
        task: asyncio.Future[Any],
    ) -> None:
        """Удаляет запись single-flight и гасит необработанное исключение."""
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _get_model(
        self,
        cache_key: tuple[Any, ...],
        model: type[ModelT],
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        tags: Iterable[str] = (),
        tag_factory: Callable[[ModelT], Iterable[str]] | None = None,
        allow_not_found: bool = False,
        cache_not_found: bool = False,
    ) -> ModelT | None:
        """Возвращает модель из кэша или выполняет общий запрос.

        Реальный запрос живёт в отдельной задаче под ``asyncio.shield``:
        отмена одного из ожидающих не отменяет запрос для остальных.
        Кэш заполняется внутри задачи до её завершения, поэтому запись
        in-flight исчезает только после появления результата в кэше.
        """
        cached = self._cache.get(cache_key)
        if isinstance(cached, model):
            return cached

        if cache_not_found and self._negative_cache.get(cache_key):
            return None

        request_key = self._request_key(path, params)
        existing = self._inflight.get(request_key)
        if existing is not None:
            return await asyncio.shield(existing)

        task: asyncio.Task[Any] = asyncio.create_task(
            self._fetch_and_cache(
                cache_key,
                model,
                path,
                params=params,
                tags=tuple(tags),
                tag_factory=tag_factory,
                allow_not_found=allow_not_found,
                cache_not_found=cache_not_found,
            )
        )
        self._inflight[request_key] = task
        task.add_done_callback(
            lambda finished, key=request_key: self._finish_inflight(
                key,
                finished,
            )
        )
        return await asyncio.shield(task)

    async def _fetch_and_cache(
        self,
        cache_key: tuple[Any, ...],
        model: type[ModelT],
        path: str,
        *,
        params: Mapping[str, str | int] | None,
        tags: tuple[str, ...],
        tag_factory: Callable[[ModelT], Iterable[str]] | None,
        allow_not_found: bool,
        cache_not_found: bool,
    ) -> ModelT | None:
        """Выполняет запрос, валидирует ответ и заполняет кэш."""
        try:
            payload = await self._perform_request(path, params=params)
        except FaceitNotFound:
            if not allow_not_found:
                raise
            if cache_not_found:
                self._negative_cache.set(cache_key, True, tags=tags)
            return None

        parsed = self._validate(model, payload)
        resolved_tags = set(tags)
        if tag_factory is not None:
            resolved_tags.update(tag_factory(parsed))
        self._cache.set(cache_key, parsed, tags=resolved_tags)
        return parsed

    def _raise_if_rate_limited(self) -> None:
        """Выбрасывает FaceitRateLimit, если глобальная пауза ещё активна."""
        now = self._clock()
        if now < self._rate_limited_until:
            raise FaceitRateLimit(
                retry_after=self._rate_limited_until - now,
            )

    def _extend_rate_limit_cooldown(
        self,
        retry_after: float | None,
    ) -> None:
        """Продлевает глобальную паузу: max(Retry-After, базовая), но с потолком."""
        cooldown = (
            self._rate_limit_cooldown
            if retry_after is None
            else max(retry_after, self._rate_limit_cooldown)
        )
        cooldown = min(cooldown, self._rate_limit_cooldown_max)
        self._rate_limited_until = max(
            self._rate_limited_until,
            self._clock() + cooldown,
        )

    async def _perform_request(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        """Выполняет HTTP-запрос с учётом глобального cooldown после 429."""
        self._raise_if_rate_limited()

        for attempt in range(self._max_retries + 1):
            # Токен тратится только на реальный сетевой запрос; попадания
            # в кэш сюда не доходят. Ждём токен вне семафора, чтобы пауза
            # не занимала слот параллелизма.
            await self._token_bucket.acquire()
            async with self._semaphore:
                # Пока запрос ждал слот, другой запрос мог получить 429.
                self._raise_if_rate_limited()
                try:
                    response = await self._client.get(path, params=params)
                except httpx.TimeoutException as exc:
                    raise FaceitError(
                        "Faceit API: истёк таймаут запроса"
                    ) from exc
                except httpx.RequestError as exc:
                    raise FaceitError("Faceit API: сетевая ошибка") from exc

            if response.status_code == 404:
                raise FaceitNotFound(f"Faceit API: ресурс не найден ({path})")

            if response.status_code == 429:
                retry_after = self._retry_after(response)
                self._extend_rate_limit_cooldown(retry_after)
                remaining = max(
                    0.0,
                    self._rate_limited_until - self._clock(),
                )
                logger.warning(
                    "Faceit API вернул 429, включаем глобальную паузу "
                    "%.2f с — запрос не повторяем",
                    remaining,
                )
                raise FaceitRateLimit(retry_after=remaining)

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

        Ответ 404 кэшируется на ``negative_cache_ttl`` секунд, чтобы
        повторные запросы не тратили квоту Faceit. Инвалидация по SteamID64
        удаляет и негативную запись.

        Args:
            steam64: SteamID64 игрока.

        Returns:
            Словарь с данными игрока либо ``None``, если игрок не найден.

        Raises:
            FaceitRateLimit: Если Faceit вернул 429 или активен глобальный
                cooldown.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("player_by_steam_id", steam64)
        player = await self._get_model(
            cache_key,
            FaceitPlayer,
            "/players",
            params={"game": "cs2", "game_player_id": steam64},
            tags=(f"steam:{steam64}",),
            tag_factory=lambda value: (f"player:{value.player_id}",),
            allow_not_found=True,
            cache_not_found=True,
        )
        if player is None:
            self._steam_to_player.pop(steam64, None)
            return None

        self._remember_player(steam64, player.player_id)
        return player.model_dump()

    def peek_player_nickname(self, steam64: str) -> str | None:
        """Возвращает ник из кэша профиля, не выполняя HTTP-запрос.

        Используется для подсказки при быстром сравнении. Кэш читается
        без изменения LRU-порядка, поэтому подсказка не влияет на вытеснение.

        Args:
            steam64: SteamID64 игрока.

        Returns:
            Ник из закэшированного профиля либо ``None``.
        """
        cached = self._cache.peek(("player_by_steam_id", steam64))
        if not isinstance(cached, FaceitPlayer):
            return None
        return cached.nickname or None

    async def get_lifetime_stats(
        self,
        player_id: str,
        *,
        steam64: str | None = None,
    ) -> dict[str, Any]:
        """Получает lifetime-статистику игрока и сегменты по картам.

        Args:
            player_id: Faceit player_id.
            steam64: Необязательный SteamID64 для точечной инвалидации
                данных игрока, даже если профиль уже вытеснен из кэша.

        Returns:
            Словарь с полями ``lifetime`` и ``segments``.

        Raises:
            FaceitNotFound: Если игрок или статистика не найдены.
            FaceitRateLimit: Если Faceit вернул 429 или активен глобальный
                cooldown.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("lifetime_stats", player_id)
        encoded_player_id = quote(player_id, safe="")
        stats = await self._get_model(
            cache_key,
            FaceitLifetimeStats,
            f"/players/{encoded_player_id}/stats/cs2",
            tags=self._player_tags(player_id, steam64),
        )
        assert stats is not None
        return stats.model_dump()

    async def get_recent_matches(
        self,
        player_id: str,
        limit: int = 20,
        *,
        steam64: str | None = None,
    ) -> dict[str, Any]:
        """Получает последние матчи игрока в CS2.

        Args:
            player_id: Faceit player_id.
            limit: Максимальное количество матчей (по умолчанию 20).
            steam64: Необязательный SteamID64 для точечной инвалидации.

        Returns:
            Словарь с ключом ``items`` — списком матчей.

        Raises:
            ValueError: Если ``limit`` меньше 1.
            FaceitNotFound: Если игрок не найден.
            FaceitRateLimit: Если Faceit вернул 429 или активен глобальный
                cooldown.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        if limit < 1:
            raise ValueError("limit должен быть больше 0")

        cache_key = ("recent_matches", player_id, limit)
        encoded_player_id = quote(player_id, safe="")
        matches = await self._get_model(
            cache_key,
            FaceitRecentMatches,
            f"/players/{encoded_player_id}/games/cs2/stats",
            params={"limit": limit},
            tags=self._player_tags(player_id, steam64),
        )
        assert matches is not None
        return matches.model_dump()

    async def get_player_ranking(
        self,
        player_id: str,
        region: str,
        *,
        country: str | None = None,
        steam64: str | None = None,
    ) -> dict[str, Any] | None:
        """Получает позицию игрока в рейтинге региона или страны.

        Args:
            player_id: Faceit player_id.
            region: Регион игрока (например, ``EU``).
            country: ISO-код страны для рейтинга страны.
            steam64: Необязательный SteamID64 для точечной инвалидации.

        Returns:
            Словарь с полем ``position`` либо ``None``, если игрок
            не найден в рейтинге.

        Raises:
            FaceitRateLimit: Если Faceit вернул 429 или активен глобальный
                cooldown.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("player_ranking", player_id, region, country)
        encoded_player_id = quote(player_id, safe="")
        encoded_region = quote(region, safe="")
        params: dict[str, str | int] = {}
        if country:
            params["country"] = country

        ranking = await self._get_model(
            cache_key,
            FaceitPlayerRanking,
            (
                f"/rankings/games/cs2/regions/{encoded_region}"
                f"/players/{encoded_player_id}"
            ),
            params=params or None,
            tags=self._player_tags(player_id, steam64),
            allow_not_found=True,
        )
        if ranking is None:
            return None
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
            FaceitRateLimit: Если Faceit вернул 429 или активен глобальный
                cooldown.
            FaceitError: При сетевой ошибке или некорректном ответе API.
        """
        cache_key = ("match_details", match_id)
        encoded_match_id = quote(match_id, safe="")
        details = await self._get_model(
            cache_key,
            FaceitMatchDetails,
            f"/matches/{encoded_match_id}",
            allow_not_found=True,
        )
        if details is None:
            return None
        return details.model_dump()
