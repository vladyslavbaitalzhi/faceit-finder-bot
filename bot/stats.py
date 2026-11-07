from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from bot.steam import SteamStats

logger = logging.getLogger(__name__)

RECENT_MATCHES_LIMIT = 20
# Один дополнительный матч нужен, чтобы посчитать ±ELO для пятого матча.
RECENT_MATCHES_FETCH_LIMIT = RECENT_MATCHES_LIMIT + 1
LAST_MATCHES_LIMIT = 5
MIN_MAP_MATCHES = 30
MAX_VALID_KD = 10.0
MAX_VALID_HEADSHOTS_PCT = 100.0
DEFAULT_FACEIT_LANGUAGE = "en"
FACEIT_LANG_PLACEHOLDER_RE = re.compile(
    r"(?:\{lang\}|%7Blang%7D)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    """Основные данные игрока для шапки сообщения."""

    player_id: str
    nickname: str
    country: str | None = None
    avatar: str | None = None
    faceit_url: str | None = None
    region: str | None = None
    faceit_elo: int | None = None
    skill_level: int | None = None
    max_elo: int | None = None

    @property
    def profile_url(self) -> str:
        """Ссылка на профиль Faceit с запасным вариантом.

        Faceit API может вернуть ``faceit_url`` с шаблоном ``{lang}``
        (или уже закодированным ``%7Blang%7D``) вместо языка. Подставляем
        язык по умолчанию, чтобы ссылка оставалась рабочей.
        """
        if self.faceit_url and self.faceit_url.startswith(
            ("http://", "https://")
        ):
            return FACEIT_LANG_PLACEHOLDER_RE.sub(
                DEFAULT_FACEIT_LANGUAGE,
                self.faceit_url,
            )
        return f"https://www.faceit.com/en/players/{quote(self.nickname, safe='')}"


@dataclass(frozen=True, slots=True)
class PlayerRanking:
    """Позиции игрока в рейтингах Faceit."""

    country_position: int | None = None
    region_position: int | None = None

    @property
    def has_data(self) -> bool:
        """Есть ли хотя бы одна позиция для вывода."""
        return self.country_position is not None or self.region_position is not None


@dataclass(frozen=True, slots=True)
class LifetimeSummary:
    """Агрегированная lifetime-статистика игрока."""

    matches: int | None = None
    winrate: float | None = None
    kd: float | None = None
    headshots_pct: float | None = None
    longest_win_streak: int | None = None
    current_win_streak: int | None = None
    current_streak: int | None = None
    current_streak_is_win: bool | None = None

    @property
    def has_data(self) -> bool:
        """Есть ли хотя бы один показатель для вывода."""
        return any(
            value is not None
            for value in (
                self.matches,
                self.winrate,
                self.kd,
                self.headshots_pct,
                self.longest_win_streak,
                self.current_streak,
            )
        )


@dataclass(frozen=True, slots=True)
class RecentForm:
    """Сводка по последним матчам игрока."""

    analyzed_matches: int
    avg_kd: float | None = None
    avg_adr: float | None = None
    avg_kr: float | None = None
    avg_kills: float | None = None
    winrate: float | None = None
    # Результаты в порядке от нового матча к старому.
    results: tuple[bool, ...] = ()

    @property
    def latest_ten(self) -> tuple[bool, ...]:
        """Результаты последних десяти матчей, новые слева."""
        return self.results[:10]

    @property
    def previous_ten(self) -> tuple[bool, ...]:
        """Результаты предыдущих десяти матчей, новые слева."""
        return self.results[10:20]

    @property
    def current_streak(self) -> tuple[int | None, bool | None]:
        """Длина текущей серии и её тип (победы/поражения)."""
        if not self.results:
            return None, None

        first_result = self.results[0]
        length = 0
        for result in self.results:
            if result != first_result:
                break
            length += 1

        return length, first_result

    @property
    def has_data(self) -> bool:
        """Есть ли хотя бы один показатель для вывода."""
        return any(
            value is not None
            for value in (
                self.avg_kd,
                self.avg_adr,
                self.avg_kr,
                self.avg_kills,
                self.winrate,
            )
        ) or bool(self.results)


@dataclass(frozen=True, slots=True)
class MapPerformance:
    """Винрейт игрока на конкретной карте."""

    name: str
    winrate: float
    matches: int

    @property
    def has_enough_matches(self) -> bool:
        """Достаточно ли матчей для сравнения с общим винрейтом."""
        return self.matches >= MIN_MAP_MATCHES


@dataclass(frozen=True, slots=True)
class MatchSummary:
    """Краткая информация об одном из последних матчей."""

    map_name: str | None = None
    score: str | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    elo_delta: int | None = None
    result: bool | None = None

    @property
    def has_data(self) -> bool:
        """Есть ли что показать в строке матча."""
        return any(
            value is not None
            for value in (
                self.map_name,
                self.score,
                self.kills,
                self.deaths,
                self.assists,
                self.elo_delta,
                self.result,
            )
        )


@dataclass(frozen=True, slots=True)
class PlayerStats:
    """Полный набор данных для формирования сообщения."""

    profile: PlayerProfile
    lifetime: LifetimeSummary
    form: RecentForm | None
    maps: tuple[MapPerformance, ...] = ()
    best_maps: tuple[MapPerformance, ...] = ()
    weak_maps: tuple[MapPerformance, ...] = ()
    ranking: PlayerRanking | None = None
    last_matches: tuple[MatchSummary, ...] = ()
    steam: SteamStats | None = None


ComparisonMetricKey = Literal[
    "elo",
    "winrate",
    "kd",
    "hs",
    "adr",
    "streak",
    "hours",
]


class ComparisonMetric(BaseModel):
    """Одна метрика в сравнительной таблице двух игроков."""

    model_config = ConfigDict(frozen=True)

    key: ComparisonMetricKey
    label: str
    left: float | None = None
    right: float | None = None
    delta: float | None = None
    higher_is_better: bool | None = True
    winner: Literal["left", "right", "tie"] | None = None


class PlayerComparison(BaseModel):
    """Готовое сравнение двух игроков."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    left: PlayerStats
    right: PlayerStats
    metrics: tuple[ComparisonMetric, ...] = ()

    @property
    def has_data(self) -> bool:
        """Есть ли хотя бы одно значение для сравнения."""
        return any(
            metric.left is not None or metric.right is not None
            for metric in self.metrics
        )



def _lookup(data: Mapping[str, Any], *keys: str) -> Any:
    """Ищет значение по ключам без учёта регистра и лишних пробелов."""
    normalized = {
        str(key).strip().casefold(): value for key, value in data.items()
    }
    for key in keys:
        value = normalized.get(key.casefold())
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> float | None:
    """Безопасно приводит значение API к конечному float."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        normalized = value.strip().replace("%", "").replace(",", ".")
        if not normalized:
            return None
        try:
            result = float(normalized)
        except ValueError:
            return None
    else:
        return None

    return result if math.isfinite(result) else None


def _to_int(value: Any) -> int | None:
    """Безопасно приводит значение API к int."""
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _to_result(value: Any) -> bool | None:
    """Преобразует результат матча Faceit в win/loss."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None

    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "win", "won", "true", "victory"}:
            return True
        if normalized in {"0", "loss", "lost", "false", "defeat"}:
            return False

    return None


def _average(values: Sequence[float]) -> float | None:
    """Возвращает среднее значение или None для пустого списка."""
    if not values:
        return None
    return sum(values) / len(values)


def _str_or_none(value: Any) -> str | None:
    """Возвращает непустую строку либо None."""
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _extract_profile(player: Mapping[str, Any]) -> PlayerProfile:
    """Собирает профиль игрока из ответа Faceit API."""
    games = player.get("games")
    cs2 = games.get("cs2") if isinstance(games, Mapping) else None
    cs2 = cs2 if isinstance(cs2, Mapping) else {}

    max_elo = _to_int(
        _lookup(
            cs2,
            "max_faceit_elo",
            "max_elo",
            "highest_elo",
            "faceit_elo_max",
            "max elo",
        )
    )
    if max_elo is None:
        max_elo = _to_int(
            _lookup(
                player,
                "max_faceit_elo",
                "max_elo",
                "highest_elo",
                "faceit_elo_max",
                "max elo",
            )
        )

    return PlayerProfile(
        player_id=str(player.get("player_id") or ""),
        nickname=str(player.get("nickname") or "Faceit-игрок"),
        country=_str_or_none(player.get("country")),
        avatar=_str_or_none(player.get("avatar")),
        faceit_url=_str_or_none(player.get("faceit_url")),
        region=_str_or_none(cs2.get("region") or player.get("region")),
        faceit_elo=_to_int(cs2.get("faceit_elo")),
        skill_level=_to_int(cs2.get("skill_level")),
        max_elo=max_elo,
    )


def _build_lifetime(lifetime_payload: Mapping[str, Any] | None) -> LifetimeSummary:
    """Извлекает общие показатели из lifetime-статистики."""
    if not lifetime_payload:
        return LifetimeSummary()

    lifetime = lifetime_payload.get("lifetime")
    if not isinstance(lifetime, Mapping):
        return LifetimeSummary()

    matches = _to_int(
        _lookup(lifetime, "Matches", "Total Matches", "Total Matches Played")
    )
    wins = _to_int(_lookup(lifetime, "Wins", "Total Wins"))

    raw_winrate = _lookup(lifetime, "Win Rate %", "Win Rate")
    winrate = _to_float(raw_winrate)
    if winrate is None and matches and wins is not None:
        winrate = wins / matches * 100
    if winrate is not None and not 0 <= winrate <= 100:
        logger.warning(
            "Lifetime winrate вне границ 0–100%%: %r — строка пропущена",
            raw_winrate,
        )
        winrate = None

    raw_kd = _lookup(lifetime, "Average K/D Ratio")
    kd = _to_float(raw_kd)
    if kd is not None and not 0 <= kd <= MAX_VALID_KD:
        logger.warning(
            "Lifetime K/D вне границ 0–%s: %r — строка пропущена",
            MAX_VALID_KD,
            raw_kd,
        )
        kd = None

    raw_headshots = _lookup(lifetime, "Average Headshots %")
    headshots_pct = _to_float(raw_headshots)
    if headshots_pct is not None and not (
        0 <= headshots_pct <= MAX_VALID_HEADSHOTS_PCT
    ):
        logger.warning(
            "Lifetime HS%% вне границ 0–%s: %r — строка пропущена",
            MAX_VALID_HEADSHOTS_PCT,
            raw_headshots,
        )
        headshots_pct = None

    return LifetimeSummary(
        matches=matches,
        winrate=winrate,
        kd=kd,
        headshots_pct=headshots_pct,
        longest_win_streak=_to_int(
            _lookup(
                lifetime,
                "Longest Win Streak",
                "Best Win Streak",
            )
        ),
        current_win_streak=_to_int(
            _lookup(
                lifetime,
                "Current Win Streak",
                "Current Winning Streak",
            )
        ),
    )


def _match_stats(match: Mapping[str, Any]) -> Mapping[str, Any]:
    """Возвращает блок stats одного матча."""
    stats = match.get("stats")
    return stats if isinstance(stats, Mapping) else {}


def extract_match_id(match: Mapping[str, Any]) -> str | None:
    """Возвращает match_id из элемента истории матчей."""
    stats = _match_stats(match)
    return _str_or_none(
        match.get("match_id")
        or _lookup(stats, "Match Id", "Match ID", "MatchId")
    )


def _build_recent_form(
    recent_payload: Mapping[str, Any] | None,
) -> RecentForm | None:
    """Считает средние показатели и форму по последним матчам."""
    if not recent_payload:
        return None

    raw_items = recent_payload.get("items")
    if not isinstance(raw_items, list):
        return None

    items = [item for item in raw_items if isinstance(item, Mapping)]
    items = items[:RECENT_MATCHES_LIMIT]
    if not items:
        return None

    kd_values: list[float] = []
    adr_values: list[float] = []
    kr_values: list[float] = []
    kill_values: list[float] = []
    results: list[bool] = []

    for match in items:
        stats = _match_stats(match)

        kd = _to_float(_lookup(stats, "K/D Ratio"))
        kills = _to_float(_lookup(stats, "Kills", "Total Kills"))
        deaths = _to_float(_lookup(stats, "Deaths", "Total Deaths"))
        if kd is None and kills is not None and deaths:
            kd = kills / deaths
        if kd is not None:
            kd_values.append(kd)

        adr = _to_float(_lookup(stats, "ADR", "Average Damage per Round"))
        if adr is not None:
            adr_values.append(adr)

        kr = _to_float(_lookup(stats, "K/R Ratio", "K/R"))
        if kr is not None:
            kr_values.append(kr)

        if kills is not None:
            kill_values.append(kills)

        result = _to_result(_lookup(stats, "Result", "Win"))
        if result is not None:
            results.append(result)

    winrate = (
        sum(1 for result in results if result) / len(results) * 100
        if results
        else None
    )

    return RecentForm(
        analyzed_matches=len(items),
        avg_kd=_average(kd_values),
        avg_adr=_average(adr_values),
        avg_kr=_average(kr_values),
        avg_kills=_average(kill_values),
        winrate=winrate,
        results=tuple(results),
    )


def _extract_map_performances(
    lifetime_payload: Mapping[str, Any] | None,
) -> list[MapPerformance]:
    """Извлекает винрейт по всем картам из сегментов статистики."""
    if not lifetime_payload:
        return []

    segments = lifetime_payload.get("segments")
    if not isinstance(segments, list):
        return []

    maps: list[MapPerformance] = []
    seen: set[str] = set()

    for segment in segments:
        if not isinstance(segment, Mapping):
            continue

        label = _str_or_none(segment.get("label"))
        if label is None or label.casefold() in seen:
            continue

        segment_type = str(segment.get("type") or "").strip().casefold()
        if segment_type != "map" and not label.casefold().startswith("de_"):
            continue

        stats = segment.get("stats")
        if not isinstance(stats, Mapping):
            continue

        matches = _to_int(_lookup(stats, "Matches", "Total Matches"))
        if matches is None or matches < 1:
            continue

        wins = _to_int(_lookup(stats, "Wins", "Total Wins"))
        winrate = _to_float(_lookup(stats, "Win Rate %", "Win Rate"))
        if winrate is None and wins is not None:
            winrate = wins / matches * 100
        if winrate is None or not 0 <= winrate <= 100:
            continue

        maps.append(
            MapPerformance(name=label, winrate=winrate, matches=matches)
        )
        seen.add(label.casefold())

    return maps


def _ranking_position(payload: Mapping[str, Any] | None) -> int | None:
    """Извлекает позицию из ответа рейтинга Faceit."""
    if not isinstance(payload, Mapping):
        return None

    position = _to_int(payload.get("position"))
    if position is None or position < 1:
        return None
    return position


def build_player_ranking(
    country_payload: Mapping[str, Any] | None,
    region_payload: Mapping[str, Any] | None,
) -> PlayerRanking | None:
    """Собирает позиции игрока в рейтинге страны и региона."""
    country_position = _ranking_position(country_payload)
    region_position = _ranking_position(region_payload)

    if country_position is None and region_position is None:
        return None

    return PlayerRanking(
        country_position=country_position,
        region_position=region_position,
    )


def _match_elo(match: Mapping[str, Any]) -> int | None:
    """Извлекает абсолютное значение ELO из элемента истории матчей."""
    stats = _match_stats(match)
    raw_value = match.get("elo")
    if raw_value is None:
        raw_value = _lookup(stats, "Elo", "ELO")
    return _to_int(raw_value)


def _explicit_elo_delta(match: Mapping[str, Any]) -> int | None:
    """Ищет явно переданное изменение ELO в элементе матча."""
    stats = _match_stats(match)
    for key in ("elo_delta", "elo_change"):
        value = _to_int(match.get(key))
        if value is not None:
            return value
    return _to_int(
        _lookup(stats, "Elo Delta", "Elo Change", "ELO Change")
    )


def _player_faction(
    match_details: Mapping[str, Any],
    player_id: str,
) -> str | None:
    """Определяет фракцию игрока в деталях матча."""
    teams = match_details.get("teams")
    if not isinstance(teams, Mapping):
        return None

    for faction, team in teams.items():
        if not isinstance(team, Mapping):
            continue
        roster = team.get("roster")
        if not isinstance(roster, list):
            continue
        for member in roster:
            if not isinstance(member, Mapping):
                continue
            if str(member.get("player_id") or "") == player_id:
                return str(faction)

    return None


def _extract_score(
    match_details: Mapping[str, Any] | None,
    player_id: str,
    result: bool | None,
) -> str | None:
    """Формирует счёт матча с точки зрения игрока."""
    if not isinstance(match_details, Mapping):
        return None

    results = match_details.get("results")
    if not isinstance(results, Mapping):
        return None

    raw_score = results.get("score")
    if not isinstance(raw_score, Mapping) or len(raw_score) < 2:
        return None

    scores = {
        str(key): _to_int(value) for key, value in raw_score.items()
    }
    if any(value is None for value in scores.values()):
        return None

    faction = _player_faction(match_details, player_id)
    if faction in scores:
        other_faction = next(
            key for key in scores if key != faction
        )
        first = scores[faction]
        second = scores[other_faction]
    else:
        values = [value for value in scores.values() if value is not None]
        if len(values) < 2:
            return None
        if result is True:
            first, second = max(values), min(values)
        elif result is False:
            first, second = min(values), max(values)
        else:
            first, second = values[0], values[1]

    return f"{first}:{second}"


def _build_last_matches(
    recent_payload: Mapping[str, Any] | None,
    match_details: Mapping[str, Mapping[str, Any]] | None,
    player_id: str,
) -> tuple[MatchSummary, ...]:
    """Собирает компактную сводку по последним матчам."""
    if not recent_payload:
        return ()

    raw_items = recent_payload.get("items")
    if not isinstance(raw_items, list):
        return ()

    all_items = [
        item for item in raw_items if isinstance(item, Mapping)
    ][: LAST_MATCHES_LIMIT + 1]
    if not all_items:
        return ()

    details = match_details or {}
    summaries: list[MatchSummary] = []

    for index, match in enumerate(all_items[:LAST_MATCHES_LIMIT]):
        stats = _match_stats(match)
        result = _to_result(_lookup(stats, "Result", "Win"))
        match_id = extract_match_id(match)
        details_for_match = (
            details.get(match_id) if match_id is not None else None
        )

        elo_delta = _explicit_elo_delta(match)
        if elo_delta is None and index + 1 < len(all_items):
            current_elo = _match_elo(match)
            previous_elo = _match_elo(all_items[index + 1])
            if current_elo is not None and previous_elo is not None:
                elo_delta = current_elo - previous_elo

        summary = MatchSummary(
            map_name=_str_or_none(_lookup(stats, "Map")),
            score=_extract_score(details_for_match, player_id, result),
            kills=_to_int(_lookup(stats, "Kills", "Total Kills")),
            deaths=_to_int(_lookup(stats, "Deaths", "Total Deaths")),
            assists=_to_int(_lookup(stats, "Assists", "Total Assists")),
            elo_delta=elo_delta,
            result=result,
        )
        if summary.has_data:
            summaries.append(summary)

    return tuple(summaries)


def _sort_maps(maps: Sequence[MapPerformance]) -> list[MapPerformance]:
    """Сортирует карты по винрейту, затем по числу матчей."""
    return sorted(
        maps,
        key=lambda item: (-item.winrate, -item.matches, item.name),
    )


def build_player_stats(
    player: Mapping[str, Any],
    lifetime: Mapping[str, Any] | None,
    recent_matches: Mapping[str, Any] | None,
    *,
    ranking: PlayerRanking | None = None,
    match_details: Mapping[str, Mapping[str, Any]] | None = None,
    steam: SteamStats | None = None,
) -> PlayerStats:
    """Собирает все данные игрока для последующего форматирования.

    Args:
        player: Ответ ``/players`` Faceit API.
        lifetime: Ответ ``/players/{id}/stats/cs2`` или ``None``.
        recent_matches: Ответ ``/players/{id}/games/cs2/stats`` или ``None``.
        ranking: Позиции игрока в рейтингах страны и региона.
        match_details: Детали последних матчей, ключ — ``match_id``.
        steam: Дополнительная статистика Steam.

    Returns:
        Готовый набор данных. Отсутствующие поля остаются ``None``.
    """
    profile = _extract_profile(player)
    lifetime_summary = _build_lifetime(lifetime)
    form = _build_recent_form(recent_matches)

    if form is not None:
        streak_length, streak_is_win = form.current_streak
    else:
        streak_length, streak_is_win = None, None

    if streak_length is not None and streak_is_win is True:
        # Lifetime-значение может быть длиннее окна последних 20 матчей.
        if lifetime_summary.current_win_streak is not None:
            streak_length = max(
                streak_length,
                lifetime_summary.current_win_streak,
            )
        lifetime_summary = replace(
            lifetime_summary,
            current_streak=streak_length,
            current_streak_is_win=True,
        )
    elif streak_length is not None and streak_is_win is False:
        lifetime_summary = replace(
            lifetime_summary,
            current_streak=streak_length,
            current_streak_is_win=False,
        )
    elif lifetime_summary.current_win_streak:
        lifetime_summary = replace(
            lifetime_summary,
            current_streak=lifetime_summary.current_win_streak,
            current_streak_is_win=True,
        )

    maps = _sort_maps(_extract_map_performances(lifetime))
    overall_winrate = lifetime_summary.winrate

    if overall_winrate is None:
        best_maps: tuple[MapPerformance, ...] = ()
        weak_maps: tuple[MapPerformance, ...] = ()
    else:
        best_maps = tuple(
            sorted(
                (
                    item
                    for item in maps
                    if item.has_enough_matches
                    and item.winrate > overall_winrate
                ),
                key=lambda item: (-item.winrate, -item.matches, item.name),
            )
        )
        weak_maps = tuple(
            sorted(
                (
                    item
                    for item in maps
                    if item.has_enough_matches
                    and item.winrate < overall_winrate
                ),
                key=lambda item: (item.winrate, -item.matches, item.name),
            )
        )

    player_id = str(player.get("player_id") or "")

    return PlayerStats(
        profile=profile,
        lifetime=lifetime_summary,
        form=form,
        maps=tuple(maps),
        best_maps=best_maps,
        weak_maps=weak_maps,
        ranking=ranking,
        last_matches=_build_last_matches(
            recent_matches,
            match_details,
            player_id,
        ),
        steam=steam,
    )


def _signed_streak(stats: PlayerStats) -> int | None:
    """Возвращает текущую серию как число: победы > 0, поражения < 0."""
    length = stats.lifetime.current_streak
    is_win = stats.lifetime.current_streak_is_win
    if length is None or is_win is None or length < 1:
        return None
    return length if is_win else -length


def _comparison_metric(
    key: ComparisonMetricKey,
    label: str,
    left: float | int | None,
    right: float | int | None,
    *,
    higher_is_better: bool | None = True,
) -> ComparisonMetric:
    """Создаёт строку сравнения и определяет победителя по метрике."""
    left_value = None if left is None else float(left)
    right_value = None if right is None else float(right)

    delta: float | None = None
    winner: Literal["left", "right", "tie"] | None = None

    if left_value is not None and right_value is not None:
        delta = left_value - right_value
        if higher_is_better is not None:
            if delta > 0:
                winner = "left"
            elif delta < 0:
                winner = "right"
            else:
                winner = "tie"

    return ComparisonMetric(
        key=key,
        label=label,
        left=left_value,
        right=right_value,
        delta=delta,
        higher_is_better=higher_is_better,
        winner=winner,
    )


def build_player_comparison(
    left: PlayerStats,
    right: PlayerStats,
) -> PlayerComparison:
    """Сопоставляет ключевые показатели двух игроков.

    ``delta`` считается как значение первого игрока минус значение второго.
    Для часов в CS2 победитель не определяется: это нейтральный показатель.
    """
    left_form = left.form
    right_form = right.form
    left_steam = left.steam
    right_steam = right.steam

    metrics = (
        _comparison_metric(
            "elo",
            "ELO",
            left.profile.faceit_elo,
            right.profile.faceit_elo,
        ),
        _comparison_metric(
            "winrate",
            "Винрейт",
            left.lifetime.winrate,
            right.lifetime.winrate,
        ),
        _comparison_metric(
            "kd",
            "K/D",
            left.lifetime.kd,
            right.lifetime.kd,
        ),
        _comparison_metric(
            "hs",
            "HS%",
            left.lifetime.headshots_pct,
            right.lifetime.headshots_pct,
        ),
        _comparison_metric(
            "adr",
            "ADR",
            left_form.avg_adr if left_form is not None else None,
            right_form.avg_adr if right_form is not None else None,
        ),
        _comparison_metric(
            "streak",
            "Текущая серия",
            _signed_streak(left),
            _signed_streak(right),
        ),
        _comparison_metric(
            "hours",
            "CS2",
            left_steam.cs2_hours if left_steam is not None else None,
            right_steam.cs2_hours if right_steam is not None else None,
            higher_is_better=None,
        ),
    )

    return PlayerComparison(left=left, right=right, metrics=metrics)

