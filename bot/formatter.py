from __future__ import annotations

import html
from collections.abc import Callable
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.stats import (
    MIN_MAP_MATCHES,
    MapPerformance,
    MatchSummary,
    PlayerProfile,
    PlayerStats,
)

MAX_MESSAGE_LENGTH = 4096
MAX_NICKNAME_LENGTH = 64
MAX_MAP_NAME_LENGTH = 48
MAX_URL_LENGTH = 512


def _truncate(value: str, max_length: int) -> str:
    """Обрезает строку, добавляя многоточие при необходимости."""
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 1].rstrip()}…"


def _escape(value: str, *, max_length: int) -> str:
    """Экранирует текст для Telegram HTML."""
    return html.escape(_truncate(value, max_length), quote=False)


def _country_flag(country: str | None) -> str | None:
    """Преобразует ISO-код страны в эмодзи-флаг."""
    if country is None:
        return None

    code = country.strip().upper()
    if len(code) != 2 or not all("A" <= char <= "Z" for char in code):
        return None

    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code)


def _country_label(country: str | None) -> str | None:
    """Возвращает флаг и код страны для блока рейтинга."""
    flag = _country_flag(country)
    if flag is None or country is None:
        return None

    code = country.strip().upper()
    return f"{flag} {code}"


def _region_label(region: str | None) -> str | None:
    """Возвращает код региона Faceit в верхнем регистре."""
    if region is None:
        return None

    code = region.strip().upper()
    return code or None


def _format_float(value: float | None, digits: int) -> str | None:
    """Форматирует число с фиксированным количеством знаков."""
    if value is None:
        return None
    return f"{value:.{digits}f}"


def _format_percent(value: float | None) -> str | None:
    """Форматирует процент без лишнего нуля после запятой."""
    if value is None:
        return None

    rounded = round(value, 1)
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def _matches_word(count: int) -> str:
    """Склоняет слово «матч» после числа."""
    if count % 10 == 1 and count % 100 != 11:
        return "матч"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "матча"
    return "матчей"


def _plural(
    count: int,
    one: str,
    few: str,
    many: str,
) -> str:
    """Выбирает правильную форму слова после числа."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return few
    return many


def _format_trend(
    lifetime_value: float | None,
    recent_value: float | None,
    formatter: Callable[[float], str | None],
) -> str:
    """Формирует сравнение lifetime-показателя с последними 20 матчами."""
    if lifetime_value is None or recent_value is None:
        return ""

    formatted = formatter(recent_value)
    if formatted is None:
        return ""

    arrow = ""
    if recent_value > lifetime_value:
        arrow = " ↑"
    elif recent_value < lifetime_value:
        arrow = " ↓"

    return f" (20 матчей: {formatted}{arrow})"


def _format_streak(length: int | None, is_win: bool | None) -> str | None:
    """Формирует строку текущей серии побед или поражений."""
    if length is None or is_win is None or length < 1:
        return None

    if is_win:
        word = _plural(length, "победа", "победы", "побед")
    else:
        word = _plural(length, "поражение", "поражения", "поражений")
    return f"• Текущая серия: {length} {word}"


def _results_sequence(results: tuple[bool, ...]) -> str:
    """Превращает результаты матчей в строку ✅/❌."""
    return "".join("✅" if result else "❌" for result in results)


def _format_map_line(
    map_stats: MapPerformance,
    *,
    is_best: bool,
    is_weak: bool,
) -> str | None:
    """Формирует строку компактной таблицы карт."""
    winrate = _format_percent(map_stats.winrate)
    if winrate is None:
        return None

    name = _escape(map_stats.name, max_length=MAX_MAP_NAME_LENGTH)
    if is_best:
        marker = "🏆"
    elif is_weak:
        marker = "📉"
    elif not map_stats.has_enough_matches:
        marker = "⚠️"
    else:
        marker = "▫️"

    return (
        f"{marker} {name} — {winrate} · "
        f"{map_stats.matches} {_matches_word(map_stats.matches)}"
    )


def _format_number(value: int | None) -> str:
    """Форматирует число K/D/A, заменяя отсутствующее значение прочерком."""
    return "—" if value is None else str(value)


def _format_elo_delta(delta: int | None) -> str | None:
    """Форматирует изменение ELO со знаком."""
    if delta is None:
        return None
    if delta > 0:
        return f"+{delta} ELO"
    if delta < 0:
        return f"−{abs(delta)} ELO"
    return "0 ELO"


def _format_match_line(match: MatchSummary) -> str | None:
    """Формирует компактную строку одного последнего матча."""
    parts: list[str] = []

    if match.map_name:
        parts.append(_escape(match.map_name, max_length=MAX_MAP_NAME_LENGTH))
    if match.score:
        parts.append(match.score)

    if any(
        value is not None
        for value in (match.kills, match.deaths, match.assists)
    ):
        parts.append(
            f"{_format_number(match.kills)}/"
            f"{_format_number(match.deaths)}/"
            f"{_format_number(match.assists)}"
        )

    elo_delta = _format_elo_delta(match.elo_delta)
    if elo_delta is not None:
        parts.append(elo_delta)

    if not parts:
        return None
    return "• " + " · ".join(parts)


def _format_hours(hours: float | None) -> str | None:
    """Форматирует количество часов в CS2."""
    if hours is None:
        return None
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))} ч"
    return f"{hours:.1f} ч"


def _limit_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Обрезает сообщение по границе строки, сохраняя HTML-разметку.

    Все строки сообщения содержат сбалансированные HTML-теги, поэтому
    обрезка по последнему переводу строки не ломает разметку.
    """
    if len(text) <= limit:
        return text

    suffix = "\n\n<i>Сообщение сокращено.</i>"
    cutoff = limit - len(suffix)
    head = text[:cutoff]
    line_break = head.rfind("\n")
    if line_break > 0:
        head = head[:line_break]
    return f"{head.rstrip()}{suffix}"


def format_player_stats(
    stats: PlayerStats,
    *,
    updated_at: datetime | None = None,
) -> str:
    """Формирует Telegram HTML-сообщение со статистикой игрока.

    Args:
        stats: Собранные данные профиля, lifetime и последних матчей.
        updated_at: Момент обновления для нижней подписи.

    Returns:
        HTML-текст длиной не более 4096 символов.
    """
    profile = stats.profile
    nickname = _escape(profile.nickname, max_length=MAX_NICKNAME_LENGTH)
    profile_url = html.escape(
        _truncate(profile.profile_url, MAX_URL_LENGTH),
        quote=True,
    )

    header = f'🎮 <b><a href="{profile_url}">{nickname}</a></b>'
    flag = _country_flag(profile.country)
    if flag is not None:
        header = f"{header} {flag}"

    header_lines: list[str] = [header]

    badge_parts: list[str] = []
    if profile.skill_level is not None:
        badge_parts.append(f"Уровень {profile.skill_level}")
    if profile.faceit_elo is not None:
        badge_parts.append(f"{profile.faceit_elo} ELO")
    if badge_parts:
        header_lines.append(f"🏅 {' · '.join(badge_parts)}")

    if profile.max_elo is not None:
        header_lines.append(f"📈 Максимальный ELO: {profile.max_elo}")

    sections: list[str] = ["\n".join(header_lines)]

    ranking = stats.ranking
    if ranking is not None and ranking.has_data:
        ranking_lines: list[str] = []
        country_label = _country_label(profile.country)
        region_label = _region_label(profile.region)
        if ranking.country_position is not None and country_label is not None:
            ranking_lines.append(f"• Страна: {country_label}")
        if ranking.region_position is not None and region_label is not None:
            ranking_lines.append(f"• Регион: {region_label}")
        if ranking_lines:
            sections.append(
                "🌍 <b>Рейтинг</b>\n" + "\n".join(ranking_lines)
            )

    overall_lines: list[str] = []
    lifetime = stats.lifetime
    form = stats.form

    if lifetime.matches is not None:
        overall_lines.append(f"• Матчей: {lifetime.matches}")

    winrate = _format_percent(lifetime.winrate)
    if winrate is not None:
        winrate_trend = _format_trend(
            lifetime.winrate,
            form.winrate if form is not None else None,
            _format_percent,
        )
        overall_lines.append(f"• Винрейт: {winrate}{winrate_trend}")

    kd = _format_float(lifetime.kd, 2)
    if kd is not None:
        kd_trend = _format_trend(
            lifetime.kd,
            form.avg_kd if form is not None else None,
            lambda value: _format_float(value, 2),
        )
        overall_lines.append(f"• K/D: {kd}{kd_trend}")

    headshots = _format_percent(lifetime.headshots_pct)
    if headshots is not None:
        overall_lines.append(f"• HS%: {headshots}")

    if lifetime.longest_win_streak is not None:
        overall_lines.append(
            f"• Лучшая серия побед: {lifetime.longest_win_streak}"
        )

    streak_line = _format_streak(
        lifetime.current_streak,
        lifetime.current_streak_is_win,
    )
    if streak_line is not None:
        overall_lines.append(streak_line)

    if overall_lines:
        sections.append(
            "📊 <b>Общая статистика</b>\n" + "\n".join(overall_lines)
        )

    if form is not None and form.has_data:
        form_lines: list[str] = []

        avg_kd = _format_float(form.avg_kd, 2)
        if avg_kd is not None:
            form_lines.append(f"• Средний K/D: {avg_kd}")

        avg_adr = _format_float(form.avg_adr, 1)
        if avg_adr is not None:
            form_lines.append(f"• Средний ADR: {avg_adr}")

        avg_kr = _format_float(form.avg_kr, 2)
        if avg_kr is not None:
            form_lines.append(f"• Средний K/R: {avg_kr}")

        avg_kills = _format_float(form.avg_kills, 1)
        if avg_kills is not None:
            form_lines.append(f"• Средние килы: {avg_kills}")

        form_winrate = _format_percent(form.winrate)
        if form_winrate is not None:
            form_lines.append(f"• Винрейт: {form_winrate}")

        if form.latest_ten:
            form_lines.append(
                "• Результаты последних 10 матчей: "
                f"{_results_sequence(form.latest_ten)}"
            )

        if form_lines:
            sections.append(
                "🔥 <b>Форма за последние 20 матчей</b>\n"
                + "\n".join(form_lines)
            )

    match_lines = [
        line
        for match in stats.last_matches
        if (line := _format_match_line(match)) is not None
    ]
    if match_lines:
        sections.append(
            "🕹️ <b>Последние 5 матчей</b>\n" + "\n".join(match_lines)
        )

    if stats.maps:
        best_names = {item.name for item in stats.best_maps}
        weak_names = {item.name for item in stats.weak_maps}
        map_lines = [
            line
            for item in stats.maps
            if (
                line := _format_map_line(
                    item,
                    is_best=item.name in best_names,
                    is_weak=item.name in weak_names,
                )
            )
            is not None
        ]

        legend_parts: list[str] = []
        if best_names:
            legend_parts.append("🏆 лучшие")
        if weak_names:
            legend_parts.append("📉 слабые")
        if any(not item.has_enough_matches for item in stats.maps):
            legend_parts.append(
                f"⚠️ меньше {MIN_MAP_MATCHES} матчей"
            )

        map_block = "🗺️ <b>Карты</b>\n" + "\n".join(map_lines)
        if legend_parts:
            map_block += "\n<i>" + " · ".join(legend_parts) + "</i>"
        sections.append(map_block)

    steam = stats.steam
    if steam is not None and steam.has_data:
        steam_lines: list[str] = []
        hours = _format_hours(steam.cs2_hours)
        if hours is not None:
            steam_lines.append(f"• CS2: {hours}")

        if steam.vac_banned is False:
            steam_lines.append("• VAC: ✅ нет")
        elif steam.vac_banned is True:
            details: list[str] = []
            if steam.vac_bans:
                details.append(
                    f"{steam.vac_bans} "
                    f"{_plural(steam.vac_bans, 'бан', 'бана', 'банов')}"
                )
            if steam.days_since_last_ban is not None:
                details.append(
                    f"{steam.days_since_last_ban} дн. назад"
                )
            suffix = f" ({', '.join(details)})" if details else ""
            steam_lines.append(f"• VAC: ⛔ бан{suffix}")

        if steam_lines:
            sections.append("💻 <b>Steam</b>\n" + "\n".join(steam_lines))

    timestamp = (updated_at or datetime.now()).strftime("%H:%M")
    sections.append(f"<i>Обновлено {timestamp}</i>")

    return _limit_message("\n\n".join(sections))


def build_player_keyboard(
    profile: PlayerProfile,
    *,
    refresh_callback_data: str,
) -> InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру профиля и обновления статистики."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть профиль на Faceit",
                    url=profile.profile_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Обновить",
                    callback_data=refresh_callback_data,
                )
            ],
        ]
    )
