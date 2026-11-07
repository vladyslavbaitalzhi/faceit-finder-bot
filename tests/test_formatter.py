from __future__ import annotations

from datetime import datetime

from bot.formatter import (
    MAX_MESSAGE_LENGTH,
    _format_map_line,
    _limit_message,
    build_comparison_keyboard,
    build_player_keyboard,
    format_player_comparison,
    format_player_stats,
)
from bot.steam import SteamStats
from bot.stats import (
    LifetimeSummary,
    MapPerformance,
    MatchSummary,
    PlayerComparison,
    PlayerProfile,
    PlayerRanking,
    PlayerStats,
    RecentForm,
    build_player_comparison,
)

PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"
FACEIT_URL = "https://www.faceit.com/ru/players/TestPlayer"
UPDATED_AT = datetime(2026, 11, 7, 15, 42)


def make_stats() -> PlayerStats:
    return PlayerStats(
        profile=PlayerProfile(
            player_id=PLAYER_ID,
            nickname="TestPlayer",
            country="kz",
            faceit_url=FACEIT_URL,
            region="EU",
            faceit_elo=2345,
            skill_level=10,
            max_elo=2600,
        ),
        lifetime=LifetimeSummary(
            matches=120,
            winrate=55,
            kd=1.25,
            headshots_pct=48,
            longest_win_streak=10,
            current_win_streak=3,
            current_streak=2,
            current_streak_is_win=True,
        ),
        form=RecentForm(
            analyzed_matches=20,
            avg_kd=1.3,
            avg_adr=77.4,
            avg_kr=0.87,
            avg_kills=20.5,
            winrate=60,
            results=(True, False) * 10,
        ),
        ranking=PlayerRanking(country_position=12, region_position=345),
        maps=(
            MapPerformance("de_overpass", 90, 12),
            MapPerformance("de_mirage", 75, 40),
            MapPerformance("de_nuke", 60, 35),
            MapPerformance("de_dust2", 55, 30),
            MapPerformance("de_inferno", 30, 31),
        ),
        best_maps=(
            MapPerformance("de_mirage", 75, 40),
            MapPerformance("de_nuke", 60, 35),
        ),
        weak_maps=(MapPerformance("de_inferno", 30, 31),),
        last_matches=(
            MatchSummary(
                map_name="de_mirage",
                score="13:9",
                kills=25,
                deaths=12,
                assists=5,
                elo_delta=20,
                result=True,
            ),
            MatchSummary(
                map_name="de_dust2",
                score="7:13",
                kills=15,
                deaths=20,
                assists=3,
                elo_delta=-18,
                result=False,
            ),
        ),
        steam=SteamStats(cs2_hours=1234.5, vac_banned=False),
    )


def test_format_player_stats_contains_all_blocks() -> None:
    text = format_player_stats(make_stats(), updated_at=UPDATED_AT)

    assert "TestPlayer" in text
    assert "🇰🇿" in text
    assert "🏅 Уровень 10 · 2345 ELO" in text
    assert "📈 Максимальный ELO: 2600" in text
    assert f'href="{FACEIT_URL}"' in text

    assert "🌍 <b>Рейтинг</b>" in text
    assert "• Страна: 🇰🇿 KZ" in text
    assert "• Регион: EU" in text

    assert "📊 <b>Общая статистика</b>" in text
    assert "• Матчей: 120" in text
    assert "• Винрейт: 55% (20 матчей: 60% ↑)" in text
    assert "• K/D: 1.25 (20 матчей: 1.30 ↑)" in text
    assert "• HS%: 48%" in text
    assert "• Лучшая серия побед: 10" in text
    assert "• Текущая серия: 2 победы" in text

    assert "🔥 <b>Форма за последние 20 матчей</b>" in text
    assert "• Средний K/D: 1.30" in text
    assert "• Средний ADR: 77.4" in text
    assert "• Средний K/R: 0.87" in text
    assert "• Средние килы: 20.5" in text
    assert "• Винрейт: 60%" in text
    assert "• Результаты последних 10 матчей: ✅❌✅❌✅❌✅❌✅❌" in text
    assert "• Предыдущие 10" not in text

    assert "🕹️ <b>Последние 5 матчей</b>" in text
    assert "• de_mirage · 13:9 · 25/12/5 · +20 ELO" in text
    assert "• de_dust2 · 7:13 · 15/20/3 · −18 ELO" in text

    assert "🗺️ <b>Карты</b>" in text
    assert "🏆 de_mirage — 75% · 40 матчей" in text
    assert "🏆 de_nuke — 60% · 35 матчей" in text
    assert "▫️ de_dust2 — 55% · 30 матчей" in text
    assert "📉 de_inferno — 30% · 31 матч" in text
    assert "⚠️ de_overpass — 90% · 12 матчей" in text
    assert "🏆 лучшие · 📉 слабые · ⚠️ меньше 30 матчей" in text

    assert "💻 <b>Steam</b>" in text
    assert "• CS2: 1234.5 ч" in text
    assert "• VAC: ✅ нет" in text

    assert "Обновлено 15:42" in text
    assert len(text) <= MAX_MESSAGE_LENGTH

    assert "Хедшоты" not in text
    assert "Худшие карты" not in text
    assert "Текущая серия побед" not in text
    assert "Уровень Faceit" not in text


def test_format_player_stats_skips_missing_values() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(
            player_id=PLAYER_ID,
            nickname="Player",
        ),
        lifetime=LifetimeSummary(),
        form=None,
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "Player" in text
    assert "Общая статистика" not in text
    assert "Форма за последние 20 матчей" not in text
    assert "Последние 5 матчей" not in text
    assert "Карты" not in text
    assert "Steam" not in text
    assert "Рейтинг" not in text
    assert "Максимальный ELO" not in text
    assert "ELO" not in text
    assert "Обновлено 15:42" in text


def test_format_ranking_shows_country_flag_code_and_region() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(
            player_id=PLAYER_ID,
            nickname="Player",
            country="kz",
            region="eu",
        ),
        lifetime=LifetimeSummary(),
        form=None,
        ranking=PlayerRanking(country_position=12, region_position=345),
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "• Страна: 🇰🇿 KZ" in text
    assert "• Регион: EU" in text


def test_format_ranking_skips_lines_without_country_or_region() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(player_id=PLAYER_ID, nickname="Player"),
        lifetime=LifetimeSummary(),
        form=None,
        ranking=PlayerRanking(country_position=12, region_position=345),
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "Рейтинг" not in text
    assert "Страна" not in text
    assert "Регион" not in text


def test_format_ranking_shows_only_available_region() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(
            player_id=PLAYER_ID,
            nickname="Player",
            region="na",
        ),
        lifetime=LifetimeSummary(),
        form=None,
        ranking=PlayerRanking(country_position=12, region_position=345),
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "Страна" not in text
    assert "• Регион: NA" in text


def test_format_player_stats_escapes_html() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(
            player_id=PLAYER_ID,
            nickname='<b>Player</b>',
            faceit_url="https://example.com/profile?a=1&b=2",
            skill_level=1,
        ),
        lifetime=LifetimeSummary(),
        form=None,
        maps=(MapPerformance("<de_map>", 75, 12),),
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "&lt;b&gt;Player&lt;/b&gt;" in text
    assert 'href="https://example.com/profile?a=1&amp;b=2"' in text
    assert "&lt;de_map&gt;" in text
    assert "Уровень 1" in text


def test_format_streak_plural_forms() -> None:
    losing = PlayerStats(
        profile=PlayerProfile(player_id=PLAYER_ID, nickname="Player"),
        lifetime=LifetimeSummary(
            current_streak=3,
            current_streak_is_win=False,
        ),
        form=None,
    )
    winning = PlayerStats(
        profile=PlayerProfile(player_id=PLAYER_ID, nickname="Player"),
        lifetime=LifetimeSummary(
            current_streak=1,
            current_streak_is_win=True,
        ),
        form=None,
    )

    assert "Текущая серия: 3 поражения" in format_player_stats(
        losing,
        updated_at=UPDATED_AT,
    )
    assert "Текущая серия: 1 победа" in format_player_stats(
        winning,
        updated_at=UPDATED_AT,
    )


def test_format_trend_down_and_equal() -> None:
    stats = PlayerStats(
        profile=PlayerProfile(player_id=PLAYER_ID, nickname="Player"),
        lifetime=LifetimeSummary(winrate=55, kd=1.25),
        form=RecentForm(
            analyzed_matches=20,
            avg_kd=1.25,
            winrate=50,
        ),
    )

    text = format_player_stats(stats, updated_at=UPDATED_AT)

    assert "• Винрейт: 55% (20 матчей: 50% ↓)" in text
    assert "• K/D: 1.25 (20 матчей: 1.25)" in text
    assert "20 матчей: 1.25 ↑" not in text
    assert "20 матчей: 1.25 ↓" not in text


def test_format_map_line_marks_short_history_as_warning_only() -> None:
    short_map = MapPerformance("de_vertigo", 10, 12)
    long_map = MapPerformance("de_ancient", 10, 40)

    short_line = _format_map_line(
        short_map,
        is_best=False,
        is_weak=False,
    )
    long_line = _format_map_line(
        long_map,
        is_best=False,
        is_weak=True,
    )

    assert short_line is not None and short_line.startswith("⚠️")
    assert long_line is not None and long_line.startswith("📉")


def test_build_player_keyboard_has_required_buttons() -> None:
    profile = PlayerProfile(
        player_id=PLAYER_ID,
        nickname="TestPlayer",
        faceit_url=FACEIT_URL,
    )

    keyboard = build_player_keyboard(
        profile,
        refresh_callback_data=f"refresh:{PLAYER_ID}",
    )

    profile_button = keyboard.inline_keyboard[0][0]
    refresh_button = keyboard.inline_keyboard[1][0]
    assert profile_button.text == "Открыть профиль на Faceit"
    assert profile_button.url == FACEIT_URL
    assert refresh_button.text == "Обновить"
    assert refresh_button.callback_data == f"refresh:{PLAYER_ID}"


def test_limit_message_keeps_html_lines_and_length() -> None:
    text = "\n\n".join(f"<b>Строка {index}</b>" for index in range(500))

    limited = _limit_message(text)

    assert len(limited) <= MAX_MESSAGE_LENGTH
    assert limited.endswith("<i>Сообщение сокращено.</i>")
    assert limited.count("<b>") == limited.count("</b>")


def make_comparison() -> PlayerComparison:
    right = PlayerStats(
        profile=PlayerProfile(
            player_id="rival-id",
            nickname="Rival",
            country="ru",
            faceit_url="https://www.faceit.com/ru/players/Rival",
            region="EU",
            faceit_elo=2100,
            skill_level=9,
        ),
        lifetime=LifetimeSummary(
            matches=100,
            winrate=60,
            kd=1.10,
            headshots_pct=52,
            longest_win_streak=8,
            current_streak=3,
            current_streak_is_win=False,
        ),
        form=RecentForm(
            analyzed_matches=20,
            avg_kd=1.2,
            avg_adr=70.2,
            avg_kr=0.8,
            avg_kills=18.0,
            winrate=55,
            results=(False, True) * 10,
        ),
        steam=SteamStats(cs2_hours=2100, vac_banned=True),
    )
    return build_player_comparison(make_stats(), right)


def test_format_player_comparison_contains_metrics_and_keyboard() -> None:
    comparison = make_comparison()
    text = format_player_comparison(comparison, updated_at=UPDATED_AT)

    assert "⚔️ <b>Сравнение игроков</b>" in text
    assert "1️⃣" in text
    assert "2️⃣" in text
    assert "🏅 Уровень 10 · 2345 ELO" in text
    assert "🏅 Уровень 9 · 2100 ELO" in text

    assert "• ELO: <b>2345</b> 🏆 vs 2100 · Δ +245" in text
    assert "• Винрейт: 55% vs <b>60%</b> 🏆 · Δ −5 п.п." in text
    assert "• K/D: <b>1.25</b> 🏆 vs 1.10 · Δ +0.15" in text
    assert "• HS%: 48% vs <b>52%</b> 🏆 · Δ −4 п.п." in text
    assert "• ADR: <b>77.4</b> 🏆 vs 70.2 · Δ +7.2" in text
    assert "• Текущая серия: <b>2 победы</b> 🏆 vs 3 поражения · Δ +5" in text
    assert "• CS2: 1234.5 ч vs 2100 ч · Δ −865.5 ч" in text

    assert "🏆 — лучший показатель" in text
    assert "Δ — разница (1-й минус 2-й)" in text
    assert "Обновлено 15:42" in text
    assert len(text) <= MAX_MESSAGE_LENGTH

    assert "Предыдущие 10" not in text

    keyboard = build_comparison_keyboard(
        comparison,
        refresh_callback_data="refresh_compare:one:two",
    )
    assert keyboard.inline_keyboard[0][0].text == "1. TestPlayer"
    assert keyboard.inline_keyboard[0][0].url == FACEIT_URL
    assert keyboard.inline_keyboard[0][1].text == "2. Rival"
    assert keyboard.inline_keyboard[0][1].url == (
        "https://www.faceit.com/ru/players/Rival"
    )
    assert keyboard.inline_keyboard[1][0].text == "🔄 Обновить сравнение"
    assert (
        keyboard.inline_keyboard[1][0].callback_data
        == "refresh_compare:one:two"
    )


def test_format_player_comparison_handles_missing_values() -> None:
    left = make_stats()
    right = PlayerStats(
        profile=PlayerProfile(
            player_id="rival-id",
            nickname="Rival",
        ),
        lifetime=LifetimeSummary(),
        form=None,
    )

    comparison = build_player_comparison(left, right)
    text = format_player_comparison(comparison, updated_at=UPDATED_AT)

    # Если значение есть только у одного игрока, победитель не определяется.
    assert "• ELO: 2345 vs —" in text
    assert "• Винрейт: 55% vs —" in text
    assert "• ADR: 77.4 vs —" in text
    assert "• CS2: 1234.5 ч vs —" in text
    assert "2345</b> 🏆 vs —" not in text


def test_format_player_comparison_without_data() -> None:
    left = PlayerStats(
        profile=PlayerProfile(player_id="left", nickname="Alpha"),
        lifetime=LifetimeSummary(),
        form=None,
    )
    right = PlayerStats(
        profile=PlayerProfile(player_id="right", nickname="Beta"),
        lifetime=LifetimeSummary(),
        form=None,
    )

    comparison = build_player_comparison(left, right)
    text = format_player_comparison(comparison, updated_at=UPDATED_AT)

    assert "Недостаточно данных для сравнения." in text
    assert "Ключевые показатели" not in text


def test_format_player_comparison_escapes_html() -> None:
    left = PlayerStats(
        profile=PlayerProfile(
            player_id="left",
            nickname="<b>Alpha</b>",
            faceit_url="https://example.com/profile?a=1&b=2",
            faceit_elo=2000,
        ),
        lifetime=LifetimeSummary(winrate=50),
        form=None,
    )
    right = PlayerStats(
        profile=PlayerProfile(
            player_id="right",
            nickname="Beta",
            faceit_url="https://example.com/profile?b=2",
            faceit_elo=1900,
        ),
        lifetime=LifetimeSummary(winrate=40),
        form=None,
    )

    comparison = build_player_comparison(left, right)
    text = format_player_comparison(comparison, updated_at=UPDATED_AT)

    assert "&lt;b&gt;Alpha&lt;/b&gt;" in text
    assert 'href="https://example.com/profile?a=1&amp;b=2"' in text
    assert "<b>Alpha</b>" not in text
    assert len(text) <= MAX_MESSAGE_LENGTH

