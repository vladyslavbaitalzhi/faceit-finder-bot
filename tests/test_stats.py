from __future__ import annotations

import logging
from typing import Any

import pytest

from bot.steam import SteamStats
from bot.stats import (
    MIN_MAP_MATCHES,
    LifetimeSummary,
    PlayerProfile,
    PlayerRanking,
    PlayerStats,
    RecentForm,
    build_player_comparison,
    build_player_ranking,
    build_player_stats,
)

PLAYER_ID = "8f3d6f0e-6a6c-4f7f-9a4b-1c2d3e4f5a6b"


@pytest.fixture
def player_payload() -> dict[str, Any]:
    return {
        "player_id": PLAYER_ID,
        "nickname": "TestPlayer",
        "country": "kz",
        "avatar": "https://example.com/avatar.png",
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


@pytest.fixture
def lifetime_payload() -> dict[str, Any]:
    return {
        "player_id": PLAYER_ID,
        "game_id": "cs2",
        "lifetime": {
            "Matches": "120",
            "Win Rate %": "55",
            "Average K/D Ratio": "1.25",
            "Average Headshots %": "48",
            "Longest Win Streak": "10",
            "Current Win Streak": "3",
        },
        "segments": [
            {
                "type": "Map",
                "label": "de_mirage",
                "stats": {"Matches": "40", "Win Rate %": "75"},
            },
            {
                "type": "Map",
                "label": "de_nuke",
                "stats": {"Matches": "35", "Win Rate %": "60"},
            },
            {
                "type": "Map",
                "label": "de_dust2",
                "stats": {"Matches": "30", "Wins": "17"},
            },
            {
                "type": "Map",
                "label": "de_ancient",
                "stats": {"Matches": "30", "Win Rate %": "50"},
            },
            {
                "type": "Map",
                "label": "de_inferno",
                "stats": {"Matches": "31", "Win Rate %": "40"},
            },
            {
                "type": "Map",
                "label": "de_overpass",
                "stats": {"Matches": "12", "Win Rate %": "90"},
            },
            {
                "type": "Map",
                "label": "de_vertigo",
                "stats": {"Matches": "30", "Win Rate %": "55"},
            },
            {
                "type": "Segment",
                "label": "all",
                "stats": {"Matches": "100", "Win Rate %": "50"},
            },
        ],
    }


@pytest.fixture
def recent_payload() -> dict[str, Any]:
    return {
        "player_id": PLAYER_ID,
        "game_id": "cs2",
        "items": [
            {
                "match_id": "match-1",
                "elo": 2400,
                "stats": {
                    "Result": "1",
                    "K/D Ratio": "1.5",
                    "ADR": "80",
                    "K/R Ratio": "0.9",
                    "Kills": "25",
                    "Deaths": "12",
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
                    "ADR": "60",
                    "K/R Ratio": "0.6",
                    "Kills": "15",
                    "Deaths": "20",
                    "Assists": "3",
                    "Map": "de_dust2",
                },
            },
            {
                "match_id": "match-3",
                "elo": 2360,
                "stats": {
                    "Result": 1,
                    "K/D Ratio": "1.0",
                    "K/R Ratio": "0.8",
                    "Kills": "20",
                    "Map": "de_nuke",
                },
            },
        ],
    }


@pytest.fixture
def match_details() -> dict[str, dict[str, Any]]:
    return {
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
                "score": {"faction1": 7, "faction2": 13},
                "winner": "faction2",
            },
            "teams": {
                "faction1": {"roster": [{"player_id": PLAYER_ID}]},
                "faction2": {"roster": [{"player_id": "other"}]},
            },
        },
    }


def test_build_player_stats_full_payload(
    player_payload: dict[str, Any],
    lifetime_payload: dict[str, Any],
    recent_payload: dict[str, Any],
    match_details: dict[str, dict[str, Any]],
) -> None:
    stats = build_player_stats(
        player_payload,
        lifetime_payload,
        recent_payload,
        match_details=match_details,
        ranking=PlayerRanking(country_position=12, region_position=345),
        steam=SteamStats(cs2_hours=100.5, vac_banned=False),
    )

    assert stats.profile.player_id == PLAYER_ID
    assert stats.profile.nickname == "TestPlayer"
    assert stats.profile.country == "kz"
    assert stats.profile.region == "EU"
    assert stats.profile.faceit_elo == 2345
    assert stats.profile.skill_level == 10
    assert stats.profile.max_elo == 2600
    assert stats.profile.profile_url == (
        "https://www.faceit.com/ru/players/TestPlayer"
    )

    assert stats.lifetime.matches == 120
    assert stats.lifetime.winrate == 55
    assert stats.lifetime.kd == 1.25
    assert stats.lifetime.headshots_pct == 48
    assert stats.lifetime.longest_win_streak == 10
    assert stats.lifetime.current_win_streak == 3

    assert stats.form is not None
    assert stats.form.analyzed_matches == 3
    assert stats.form.avg_kd == pytest.approx(1.0)
    assert stats.form.avg_adr == pytest.approx(70.0)
    assert stats.form.avg_kr == pytest.approx(0.766666, rel=1e-5)
    assert stats.form.avg_kills == pytest.approx(20.0)
    assert stats.form.winrate == pytest.approx(66.666666, rel=1e-5)
    assert stats.form.results == (True, False, True)
    assert stats.form.latest_ten == (True, False, True)
    assert stats.form.previous_ten == ()

    # Первая победа в recent-окне короче lifetime-серии, поэтому берём 3.
    assert stats.lifetime.current_streak == 3
    assert stats.lifetime.current_streak_is_win is True

    assert [item.name for item in stats.maps] == [
        "de_overpass",
        "de_mirage",
        "de_nuke",
        "de_dust2",
        "de_vertigo",
        "de_ancient",
        "de_inferno",
    ]
    assert [item.name for item in stats.best_maps] == [
        "de_mirage",
        "de_nuke",
        "de_dust2",
    ]
    assert [item.name for item in stats.weak_maps] == [
        "de_inferno",
        "de_ancient",
    ]
    assert stats.best_maps[2].winrate == pytest.approx(17 / 30 * 100)
    assert all(item.matches >= MIN_MAP_MATCHES for item in stats.best_maps)
    assert all(item.matches >= MIN_MAP_MATCHES for item in stats.weak_maps)
    assert not any(item.name == "de_overpass" for item in stats.best_maps)
    assert not any(item.name == "de_overpass" for item in stats.weak_maps)
    assert not any(item.name == "de_vertigo" for item in stats.best_maps)
    assert not any(item.name == "de_vertigo" for item in stats.weak_maps)

    overpass = next(item for item in stats.maps if item.name == "de_overpass")
    assert overpass.has_enough_matches is False

    assert stats.ranking == PlayerRanking(
        country_position=12,
        region_position=345,
    )

    assert len(stats.last_matches) == 3
    first, second, third = stats.last_matches
    assert first.map_name == "de_mirage"
    assert first.score == "13:9"
    assert (first.kills, first.deaths, first.assists) == (25, 12, 5)
    assert first.elo_delta == 20
    assert first.result is True

    assert second.map_name == "de_dust2"
    assert second.score == "7:13"
    assert second.elo_delta == 20
    assert second.result is False

    assert third.map_name == "de_nuke"
    assert third.score is None
    assert third.elo_delta is None

    assert stats.steam == SteamStats(cs2_hours=100.5, vac_banned=False)


def test_build_player_stats_without_optional_data() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        None,
    )

    assert stats.profile.nickname == "Player"
    assert stats.profile.country is None
    assert stats.profile.faceit_elo is None
    assert stats.profile.skill_level is None
    assert stats.profile.max_elo is None
    assert not stats.lifetime.has_data
    assert stats.form is None
    assert stats.maps == ()
    assert stats.best_maps == ()
    assert stats.weak_maps == ()
    assert stats.ranking is None
    assert stats.last_matches == ()
    assert stats.steam is None


def test_build_player_stats_tolerates_missing_fields() -> None:
    stats = build_player_stats(
        {
            "player_id": PLAYER_ID,
            "nickname": "Player",
            "games": {"cs2": {}},
        },
        {
            "lifetime": {
                "Matches": "not a number",
                "Win Rate %": None,
                "Average K/D Ratio": "—",
                "Average Headshots %": "n/a",
            },
            "segments": [
                {"type": "Map", "label": "de_mirage", "stats": {}},
                {"type": "Map", "label": "de_nuke"},
                "invalid segment",
            ],
        },
        {"items": [{"stats": {}}, "invalid item"]},
    )

    assert stats.lifetime.matches is None
    assert stats.lifetime.winrate is None
    assert stats.lifetime.kd is None
    assert stats.lifetime.headshots_pct is None
    assert stats.maps == ()
    assert stats.best_maps == ()
    assert stats.weak_maps == ()
    assert stats.form is not None
    assert stats.form.analyzed_matches == 1
    assert not stats.form.has_data
    assert stats.last_matches == ()


def test_lifetime_winrate_and_map_winrate_are_calculated_from_wins() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        {
            "lifetime": {
                "Matches": "20",
                "Wins": "11",
                "Average K/D Ratio": "1.0",
            },
            "segments": [
                {
                    "type": "Map",
                    "label": "de_mirage",
                    "stats": {"Matches": "30", "Wins": "18"},
                }
            ],
        },
        None,
    )

    assert stats.lifetime.winrate == pytest.approx(55.0)
    assert stats.maps[0].winrate == pytest.approx(60.0)
    assert [item.name for item in stats.best_maps] == ["de_mirage"]


def test_lifetime_sanity_bounds_skip_bad_values_and_log_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        {
            "lifetime": {
                "Matches": "8559",
                # Накопительные поля не должны подменять средние значения.
                "K/D Ratio": "3002.49",
                "Total Headshots %": "123690",
                "Average K/D Ratio": "3002.49",
                "Average Headshots %": "123690",
            }
        },
        None,
    )

    assert stats.lifetime.kd is None
    assert stats.lifetime.headshots_pct is None
    assert "3002.49" in caplog.text
    assert "123690" in caplog.text
    assert "K/D" in caplog.text
    assert "HS%" in caplog.text


def test_lifetime_uses_average_fields_only() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        {
            "lifetime": {
                "Matches": "100",
                "K/D Ratio": "3002.49",
                "Total Headshots %": "123690",
                "Average K/D Ratio": "1.18",
                "Average Headshots %": "52",
            }
        },
        None,
    )

    assert stats.lifetime.kd == pytest.approx(1.18)
    assert stats.lifetime.headshots_pct == pytest.approx(52)


def test_recent_form_limits_matches_to_twenty_and_splits_ten_by_ten() -> None:
    items = [
        {"stats": {"Result": "0" if index < 10 else "1"}}
        for index in range(25)
    ]

    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        {"items": items},
    )

    assert stats.form is not None
    assert stats.form.analyzed_matches == 20
    assert stats.form.latest_ten == (False,) * 10
    assert stats.form.previous_ten == (True,) * 10
    assert stats.form.results == (False,) * 10 + (True,) * 10
    assert stats.lifetime.current_streak == 10
    assert stats.lifetime.current_streak_is_win is False


def test_recent_form_computes_kd_from_kills_and_deaths() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        {
            "items": [
                {
                    "stats": {
                        "Kills": "20",
                        "Deaths": "10",
                        "Result": "win",
                    }
                }
            ]
        },
    )

    assert stats.form is not None
    assert stats.form.avg_kd == pytest.approx(2.0)
    assert stats.form.avg_kills == pytest.approx(20.0)
    assert stats.form.winrate == pytest.approx(100.0)
    assert stats.form.results == (True,)


def test_current_loss_streak_overrides_lifetime_win_streak() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        {"lifetime": {"Current Win Streak": "5"}},
        {
            "items": [
                {"stats": {"Result": "0"}},
                {"stats": {"Result": "0"}},
                {"stats": {"Result": "1"}},
            ]
        },
    )

    assert stats.lifetime.current_streak == 2
    assert stats.lifetime.current_streak_is_win is False


def test_current_streak_falls_back_to_lifetime_without_recent_results() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        {"lifetime": {"Current Win Streak": "4"}},
        {"items": [{"stats": {}}]},
    )

    assert stats.lifetime.current_streak == 4
    assert stats.lifetime.current_streak_is_win is True


def test_build_player_ranking_uses_country_and_region_positions() -> None:
    ranking = build_player_ranking(
        {"position": 12},
        {"position": 345},
    )

    assert ranking == PlayerRanking(
        country_position=12,
        region_position=345,
    )
    assert ranking is not None
    assert ranking.has_data is True


def test_build_player_ranking_ignores_invalid_positions() -> None:
    assert build_player_ranking(None, {"position": 0}) is None
    assert build_player_ranking({"position": "n/a"}, None) is None


def test_max_elo_falls_back_to_top_level_field() -> None:
    stats = build_player_stats(
        {
            "player_id": PLAYER_ID,
            "nickname": "Player",
            "games": {"cs2": {"faceit_elo": 2000}},
            "max elo": "2450",
        },
        None,
        None,
    )

    assert stats.profile.max_elo == 2450


def test_last_matches_use_extra_match_for_fifth_elo_delta() -> None:
    items = [
        {
            "match_id": f"match-{index}",
            "elo": 1300 - index * 10,
            "stats": {"Map": "de_mirage", "Result": "1"},
        }
        for index in range(21)
    ]

    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        {"items": items},
    )

    assert len(stats.last_matches) == 5
    assert [match.elo_delta for match in stats.last_matches] == [10] * 5


def test_explicit_elo_delta_has_priority() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        {
            "items": [
                {
                    "elo": 2400,
                    "elo_delta": -17,
                    "stats": {"Map": "de_nuke", "Result": "0"},
                },
                {"elo": 2417, "stats": {"Map": "de_nuke", "Result": "1"}},
            ]
        },
    )

    assert stats.last_matches[0].elo_delta == -17


def test_last_matches_score_uses_lower_and_higher_scores_without_roster() -> None:
    stats = build_player_stats(
        {"player_id": PLAYER_ID, "nickname": "Player"},
        None,
        {
            "items": [
                {
                    "match_id": "win",
                    "stats": {"Map": "de_mirage", "Result": "1"},
                },
                {
                    "match_id": "loss",
                    "stats": {"Map": "de_dust2", "Result": "0"},
                },
            ]
        },
        match_details={
            "win": {
                "results": {"score": {"faction1": 9, "faction2": 13}},
                "teams": {},
            },
            "loss": {
                "results": {"score": {"faction1": 11, "faction2": 7}},
                "teams": {},
            },
        },
    )

    assert stats.last_matches[0].score == "13:9"
    assert stats.last_matches[1].score == "7:11"


def test_profile_url_replaces_lang_placeholder() -> None:
    profile = PlayerProfile(
        player_id=PLAYER_ID,
        nickname="TestPlayer",
        faceit_url="https://www.faceit.com/{lang}/players/TestPlayer",
    )

    assert profile.profile_url == (
        "https://www.faceit.com/en/players/TestPlayer"
    )


def test_profile_url_replaces_encoded_lang_placeholder() -> None:
    profile = PlayerProfile(
        player_id=PLAYER_ID,
        nickname="TestPlayer",
        faceit_url="https://www.faceit.com/%7Blang%7D/players/TestPlayer",
    )

    assert profile.profile_url == (
        "https://www.faceit.com/en/players/TestPlayer"
    )


def test_profile_url_fallback() -> None:
    profile = PlayerProfile(
        player_id=PLAYER_ID,
        nickname="Test Player",
        faceit_url=None,
    )

    assert profile.profile_url == (
        "https://www.faceit.com/en/players/Test%20Player"
    )


def _make_comparison_player(
    player_id: str,
    nickname: str,
    *,
    elo: int | None = None,
    level: int | None = None,
    winrate: float | None = None,
    kd: float | None = None,
    hs: float | None = None,
    adr: float | None = None,
    streak: int | None = None,
    streak_is_win: bool | None = None,
    hours: float | None = None,
) -> PlayerStats:
    form = (
        RecentForm(analyzed_matches=20, avg_adr=adr)
        if adr is not None
        else None
    )
    steam = SteamStats(cs2_hours=hours) if hours is not None else None
    return PlayerStats(
        profile=PlayerProfile(
            player_id=player_id,
            nickname=nickname,
            faceit_elo=elo,
            skill_level=level,
        ),
        lifetime=LifetimeSummary(
            winrate=winrate,
            kd=kd,
            headshots_pct=hs,
            current_streak=streak,
            current_streak_is_win=streak_is_win,
        ),
        form=form,
        steam=steam,
    )


def test_build_player_comparison_marks_winners_and_deltas() -> None:
    left = _make_comparison_player(
        "left",
        "Alpha",
        elo=2345,
        level=10,
        winrate=55,
        kd=1.25,
        hs=48,
        adr=77.4,
        streak=2,
        streak_is_win=True,
        hours=1234.5,
    )
    right = _make_comparison_player(
        "right",
        "Beta",
        elo=2100,
        level=9,
        winrate=60,
        kd=1.10,
        hs=52,
        adr=70.2,
        streak=3,
        streak_is_win=False,
        hours=2100,
    )

    comparison = build_player_comparison(left, right)
    metrics = {metric.key: metric for metric in comparison.metrics}

    assert comparison.has_data is True

    assert metrics["elo"].winner == "left"
    assert metrics["elo"].delta == 245

    assert metrics["winrate"].winner == "right"
    assert metrics["winrate"].delta == -5

    assert metrics["kd"].winner == "left"
    assert metrics["kd"].delta == pytest.approx(0.15)

    assert metrics["hs"].winner == "right"
    assert metrics["hs"].delta == -4

    assert metrics["adr"].winner == "left"
    assert metrics["adr"].delta == pytest.approx(7.2)

    assert metrics["streak"].winner == "left"
    assert metrics["streak"].delta == 5

    assert metrics["hours"].winner is None
    assert metrics["hours"].higher_is_better is None
    assert metrics["hours"].delta == pytest.approx(-865.5)


def test_build_player_comparison_handles_missing_values_and_ties() -> None:
    left = _make_comparison_player(
        "left",
        "Alpha",
        elo=2000,
        winrate=50,
        kd=1.0,
        adr=80,
    )
    right = _make_comparison_player(
        "right",
        "Beta",
        elo=2000,
        winrate=50,
        kd=1.0,
    )

    comparison = build_player_comparison(left, right)
    metrics = {metric.key: metric for metric in comparison.metrics}

    assert metrics["elo"].winner == "tie"
    assert metrics["elo"].delta == 0
    assert metrics["winrate"].winner == "tie"
    assert metrics["kd"].winner == "tie"

    assert metrics["adr"].left == 80
    assert metrics["adr"].right is None
    assert metrics["adr"].delta is None
    assert metrics["adr"].winner is None

    assert metrics["hs"].left is None
    assert metrics["hs"].right is None
    assert metrics["hours"].left is None
    assert metrics["hours"].right is None


def test_build_player_comparison_signed_streaks() -> None:
    left = _make_comparison_player(
        "left",
        "Alpha",
        streak=2,
        streak_is_win=False,
    )
    right = _make_comparison_player(
        "right",
        "Beta",
        streak=5,
        streak_is_win=False,
    )

    comparison = build_player_comparison(left, right)
    streak = next(
        metric for metric in comparison.metrics if metric.key == "streak"
    )

    assert streak.left == -2
    assert streak.right == -5
    assert streak.delta == 3
    assert streak.winner == "left"


def test_build_player_comparison_without_data() -> None:
    left = _make_comparison_player("left", "Alpha")
    right = _make_comparison_player("right", "Beta")

    comparison = build_player_comparison(left, right)

    assert comparison.has_data is False
