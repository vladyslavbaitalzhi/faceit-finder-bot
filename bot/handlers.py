from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Mapping
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.faceit import FaceitClient, FaceitError, FaceitNotFound, FaceitRateLimit
from bot.formatter import build_player_keyboard, format_player_stats
from bot.steam import (
    STEAM_ID64_RE,
    SteamStats,
    fetch_steam_stats,
    resolve_steam_id,
)
from bot.stats import (
    LAST_MATCHES_LIMIT,
    RECENT_MATCHES_FETCH_LIMIT,
    PlayerStats,
    build_player_ranking,
    build_player_stats,
    extract_match_id,
)

logger = logging.getLogger(__name__)

router = Router(name="main")

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
REFRESH_CALLBACK_PREFIX = "refresh:"

START_MESSAGE = (
    "<b>Привет!</b> 👋\n\n"
    "Отправь ссылку на Steam-профиль — покажу статистику игрока "
    "на <b>Faceit</b> по CS2.\n\n"
    "Например: <code>https://steamcommunity.com/id/username</code>"
)

HELP_MESSAGE = (
    "<b>Как пользоваться ботом</b>\n\n"
    "1. Отправь ссылку на Steam-профиль, например:\n"
    "<code>https://steamcommunity.com/id/username</code>\n"
    "2. Бот найдёт игрока на Faceit и покажет статистику CS2.\n\n"
    "Под сообщением есть кнопки для перехода в профиль Faceit "
    "и обновления статистики.\n\n"
    "<b>Команды</b>\n"
    "/start — начать работу\n"
    "/help — показать эту справку"
)

NO_LINK_MESSAGE = (
    "Не вижу ссылки в сообщении. 🤔\n\n"
    "Пришли ссылку на Steam-профиль, например:\n"
    "<code>https://steamcommunity.com/id/username</code>"
)

INVALID_STEAM_MESSAGE = (
    "Не удалось получить SteamID64 по этой ссылке. 🤔\n\n"
    "Проверь ссылку на Steam-профиль и попробуй ещё раз."
)

PLAYER_NOT_FOUND_MESSAGE = "У этого игрока нет Faceit-аккаунта с CS2"

FACEIT_NOT_CONFIGURED_MESSAGE = (
    "Проверка Faceit не настроена: добавь FACEIT_API_KEY в .env."
)

FACEIT_RATE_LIMIT_MESSAGE = (
    "Faceit временно ограничил количество запросов. Попробуй чуть позже."
)

FACEIT_ERROR_MESSAGE = (
    "Не удалось получить статистику Faceit. Попробуй ещё раз позже."
)


def _extract_url(text: str) -> str | None:
    """Возвращает первую ссылку из сообщения."""
    match = URL_RE.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(TRAILING_PUNCTUATION)


def _unwrap_stats_response(
    result: Any,
    label: str,
) -> tuple[dict[str, Any] | None, FaceitError | None]:
    """Разбирает результат asyncio.gather для одного запроса статистики."""
    if isinstance(result, FaceitNotFound):
        return None, None

    if isinstance(result, FaceitError):
        logger.warning("Faceit API, ошибка запроса %s: %s", label, result)
        return None, result

    if isinstance(result, BaseException):
        logger.error(
            "Неожиданная ошибка запроса %s к Faceit API",
            label,
            exc_info=result,
        )
        return None, FaceitError(str(result))

    return result if isinstance(result, dict) else None, None


def _optional_payload(
    result: Any,
    label: str,
) -> Mapping[str, Any] | None:
    """Разбирает необязательный ответ Faceit API без проброса ошибки."""
    if isinstance(result, FaceitNotFound):
        return None
    if isinstance(result, BaseException):
        logger.warning("Faceit API, ошибка запроса %s: %s", label, result)
        return None
    if isinstance(result, Mapping):
        return result
    return None


async def _load_match_details(
    faceit_client: FaceitClient,
    recent: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Загружает детали последних матчей для счёта."""
    if not isinstance(recent, Mapping):
        return {}

    raw_items = recent.get("items")
    if not isinstance(raw_items, list):
        return {}

    match_ids: list[str] = []
    for item in raw_items[:LAST_MATCHES_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        match_id = extract_match_id(item)
        if match_id is not None and match_id not in match_ids:
            match_ids.append(match_id)

    if not match_ids:
        return {}

    results = await asyncio.gather(
        *(
            faceit_client.get_match_details(match_id)
            for match_id in match_ids
        ),
        return_exceptions=True,
    )

    details: dict[str, Mapping[str, Any]] = {}
    for match_id, result in zip(match_ids, results):
        if isinstance(result, Mapping):
            details[match_id] = result
        elif isinstance(result, BaseException) and not isinstance(
            result,
            FaceitNotFound,
        ):
            logger.warning(
                "Faceit API, ошибка деталей матча %s: %s",
                match_id,
                result,
            )
    return details


async def _load_player_stats(
    steam64: str,
    faceit_client: FaceitClient,
) -> PlayerStats | None:
    """Загружает профиль, статистику, рейтинги и данные Steam."""
    player = await faceit_client.get_player_by_steam_id(steam64)
    if player is None:
        return None

    player_id = str(player.get("player_id") or "")
    if not player_id:
        raise FaceitError("В ответе Faceit API нет player_id")

    games = player.get("games")
    cs2 = games.get("cs2") if isinstance(games, Mapping) else None
    cs2 = cs2 if isinstance(cs2, Mapping) else {}
    region = cs2.get("region")
    country = player.get("country")

    requests: dict[str, Awaitable[Any]] = {
        "lifetime": faceit_client.get_lifetime_stats(player_id),
        "recent": faceit_client.get_recent_matches(
            player_id,
            limit=RECENT_MATCHES_FETCH_LIMIT,
        ),
        "steam": fetch_steam_stats(steam64),
    }
    if isinstance(region, str) and region:
        requests["region_ranking"] = faceit_client.get_player_ranking(
            player_id,
            region,
        )
        if isinstance(country, str) and country:
            requests["country_ranking"] = faceit_client.get_player_ranking(
                player_id,
                region,
                country=country,
            )

    results = dict(
        zip(
            requests,
            await asyncio.gather(*requests.values(), return_exceptions=True),
        )
    )

    lifetime, lifetime_error = _unwrap_stats_response(
        results.get("lifetime"),
        "lifetime",
    )
    recent, recent_error = _unwrap_stats_response(
        results.get("recent"),
        "recent",
    )

    if lifetime_error is not None and recent_error is not None:
        raise lifetime_error

    ranking = build_player_ranking(
        _optional_payload(results.get("country_ranking"), "country_ranking"),
        _optional_payload(results.get("region_ranking"), "region_ranking"),
    )

    steam_result = results.get("steam")
    if isinstance(steam_result, SteamStats):
        steam_stats: SteamStats | None = steam_result
    else:
        steam_stats = None
        if isinstance(steam_result, BaseException):
            logger.warning(
                "Не удалось загрузить статистику Steam: %s",
                steam_result,
            )

    match_details = await _load_match_details(faceit_client, recent)

    return build_player_stats(
        player,
        lifetime,
        recent,
        ranking=ranking,
        match_details=match_details,
        steam=steam_stats,
    )


def _error_message(error: Exception) -> str:
    """Подбирает понятный пользователю текст ошибки Faceit."""
    if isinstance(error, FaceitRateLimit):
        return FACEIT_RATE_LIMIT_MESSAGE
    return FACEIT_ERROR_MESSAGE


def _build_message(
    stats: PlayerStats,
    steam64: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и inline-клавиатуру для статистики."""
    return (
        format_player_stats(stats),
        build_player_keyboard(
            stats.profile,
            refresh_callback_data=f"{REFRESH_CALLBACK_PREFIX}{steam64}",
        ),
    )


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Обрабатывает команду /start."""
    await message.answer(START_MESSAGE)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Обрабатывает команду /help."""
    await message.answer(HELP_MESSAGE)


@router.message(F.text)
async def handle_link(
    message: Message,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обрабатывает ссылку на Steam-профиль."""
    url = _extract_url(message.text or "")
    if url is None:
        await message.answer(NO_LINK_MESSAGE)
        return

    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    if message.bot is not None:
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
        )

    steam64 = await resolve_steam_id(url)
    if steam64 is None:
        await message.answer(INVALID_STEAM_MESSAGE)
        return

    try:
        stats = await _load_player_stats(steam64, faceit_client)
    except FaceitError as exc:
        logger.warning("Не удалось загрузить статистику Faceit: %s", exc)
        await message.answer(_error_message(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка при загрузке статистики")
        await message.answer(FACEIT_ERROR_MESSAGE)
        return

    if stats is None:
        await message.answer(PLAYER_NOT_FOUND_MESSAGE)
        return

    text, keyboard = _build_message(stats, steam64)
    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(REFRESH_CALLBACK_PREFIX))
async def handle_refresh(
    query: CallbackQuery,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обновляет статистику по нажатию inline-кнопки."""
    if faceit_client is None:
        await query.answer(
            FACEIT_NOT_CONFIGURED_MESSAGE,
            show_alert=True,
        )
        return

    steam64 = (query.data or "").removeprefix(REFRESH_CALLBACK_PREFIX)
    if not STEAM_ID64_RE.fullmatch(steam64):
        await query.answer(
            "Некорректные данные для обновления.",
            show_alert=True,
        )
        return

    if not isinstance(query.message, Message):
        await query.answer(
            "Сообщение устарело. Пришли ссылку на Steam-профиль ещё раз.",
            show_alert=True,
        )
        return

    await query.answer("Обновляю статистику…")

    if query.bot is not None:
        await query.bot.send_chat_action(
            chat_id=query.message.chat.id,
            action=ChatAction.TYPING,
        )

    faceit_client.clear_cache()
    try:
        stats = await _load_player_stats(steam64, faceit_client)
    except FaceitError as exc:
        logger.warning("Не удалось обновить статистику Faceit: %s", exc)
        await query.message.answer(_error_message(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка при обновлении статистики")
        await query.message.answer(FACEIT_ERROR_MESSAGE)
        return

    if stats is None:
        await query.message.edit_text(
            PLAYER_NOT_FOUND_MESSAGE,
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
        return

    text, keyboard = _build_message(stats, steam64)
    try:
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            logger.warning("Не удалось обновить сообщение: %s", exc)
            await query.message.answer(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
