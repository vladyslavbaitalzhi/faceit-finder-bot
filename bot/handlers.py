from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Mapping
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.faceit import FaceitClient, FaceitError, FaceitNotFound, FaceitRateLimit
from bot.formatter import (
    build_comparison_keyboard,
    build_player_keyboard,
    format_player_comparison,
    format_player_stats,
)
from bot.steam import (
    STEAM_ID64_RE,
    SteamStats,
    fetch_steam_stats,
    parse_steam_input,
    resolve_steam_id,
)
from bot.stats import (
    LAST_MATCHES_LIMIT,
    RECENT_MATCHES_FETCH_LIMIT,
    PlayerStats,
    build_player_comparison,
    build_player_ranking,
    build_player_stats,
    extract_match_id,
)

logger = logging.getLogger(__name__)

router = Router(name="main")

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
REFRESH_CALLBACK_PREFIX = "refresh:"
REFRESH_COMPARE_CALLBACK_PREFIX = "refresh_compare:"
COMPARE_SEPARATORS = {"vs", "vs.", "и", "and", "&", "против"}

START_MESSAGE = (
    "<b>Привет!</b> 👋\n\n"
    "Отправь ссылку на Steam-профиль — покажу статистику игрока "
    "на <b>Faceit</b> по CS2.\n\n"
    "Хочешь сравнить двух игроков? Отправь две ссылки одним сообщением "
    "или используй <code>/compare url1 url2</code>.\n\n"
    "Например: <code>https://steamcommunity.com/id/username</code>"
)

HELP_MESSAGE = (
    "<b>Как пользоваться ботом</b>\n\n"
    "1. Отправь ссылку на Steam-профиль, например:\n"
    "<code>https://steamcommunity.com/id/username</code>\n"
    "2. Бот найдёт игрока на Faceit и покажет статистику CS2.\n\n"
    "Для сравнения двух игроков отправь две ссылки одним сообщением "
    "или используй:\n"
    "<code>/compare url1 url2</code>\n\n"
    "Под сообщением есть кнопки для перехода в профиль Faceit "
    "и обновления статистики.\n\n"
    "<b>Команды</b>\n"
    "/start — начать работу\n"
    "/help — показать эту справку\n"
    "/compare url1 url2 — сравнить двух игроков"
)

NO_LINK_MESSAGE = (
    "Не вижу ссылки в сообщении. 🤔\n\n"
    "Пришли ссылку на Steam-профиль, например:\n"
    "<code>https://steamcommunity.com/id/username</code>"
)

COMPARE_USAGE_MESSAGE = (
    "<b>Сравнение игроков</b>\n\n"
    "Отправь две ссылки на Steam-профили:\n"
    "<code>/compare url1 url2</code>\n\n"
    "Или просто отправь две ссылки одним сообщением."
)

COMPARE_ONE_LINK_MESSAGE = (
    "Для сравнения нужны две ссылки на Steam-профили. "
    "Например:\n<code>/compare url1 url2</code>"
)

COMPARE_TOO_MANY_LINKS_MESSAGE = (
    "Я умею сравнивать только двух игроков. "
    "Пришли ровно две ссылки на Steam-профили."
)

COMPARE_INVALID_STEAM_MESSAGE = (
    "Не удалось получить SteamID64 для одной из ссылок. 🤔\n\n"
    "Проверь обе ссылки на Steam-профили и попробуй ещё раз."
)

COMPARE_SAME_PLAYER_MESSAGE = "Нельзя сравнивать игрока с самим собой."

COMPARE_FIRST_NOT_FOUND_MESSAGE = (
    "Не удалось сравнить: у первого игрока нет Faceit-аккаунта с CS2."
)
COMPARE_SECOND_NOT_FOUND_MESSAGE = (
    "Не удалось сравнить: у второго игрока нет Faceit-аккаунта с CS2."
)
COMPARE_FIRST_ERROR_MESSAGE = (
    "Не удалось загрузить статистику первого игрока. "
    "Попробуй ещё раз позже."
)
COMPARE_SECOND_ERROR_MESSAGE = (
    "Не удалось загрузить статистику второго игрока. "
    "Попробуй ещё раз позже."
)
COMPARE_BOTH_NOT_FOUND_MESSAGE = (
    "Ни у одного из игроков нет Faceit-аккаунта с CS2."
)
COMPARE_BOTH_ERROR_MESSAGE = (
    "Не удалось загрузить статистику игроков. Попробуй ещё раз позже."
)
COMPARE_NO_DATA_MESSAGE = "Недостаточно данных для сравнения игроков."

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


def _extract_urls(text: str) -> list[str]:
    """Возвращает все ссылки из сообщения в порядке появления."""
    return [
        match.group(0).rstrip(TRAILING_PUNCTUATION)
        for match in URL_RE.finditer(text)
    ]


def _parse_compare_inputs(raw: str) -> list[str]:
    """Разбирает аргументы /compare в список Steam-идентификаторов.

    Приоритет у ссылок. Если ссылок меньше двух, дополнительно принимаются
    голые SteamID64 и vanity-имена, которые понимает ``parse_steam_input``.
    """
    urls = _extract_urls(raw)
    if len(urls) >= 2:
        return urls

    tokens = [
        token.strip().rstrip(TRAILING_PUNCTUATION)
        for token in re.split(r"[\s,]+", raw)
        if token.strip()
    ]

    candidates: list[str] = []
    for token in tokens:
        if token.casefold() in COMPARE_SEPARATORS:
            continue
        if token.startswith(("http://", "https://")):
            candidates.append(token)
            continue
        if parse_steam_input(token) is not None:
            candidates.append(token)
    return candidates


async def _send_typing(message: Message) -> None:
    """Показывает в чате, что бот обрабатывает запрос."""
    if message.bot is not None:
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
        )


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


async def _resolve_compare_inputs(
    inputs: list[str],
) -> tuple[str, str] | str:
    """Разрешает два Steam-идентификатора в SteamID64.

    Возвращает пару SteamID64 либо готовый текст ошибки для пользователя.
    """
    if len(inputs) == 1:
        return COMPARE_ONE_LINK_MESSAGE
    if len(inputs) != 2:
        return COMPARE_TOO_MANY_LINKS_MESSAGE

    results = await asyncio.gather(
        *(resolve_steam_id(value) for value in inputs),
        return_exceptions=True,
    )

    steam_ids: list[str] = []
    for value, result in zip(inputs, results):
        if isinstance(result, BaseException):
            logger.warning(
                "Не удалось разрешить Steam-идентификатор %r: %s",
                value,
                result,
            )
            return COMPARE_INVALID_STEAM_MESSAGE
        if not isinstance(result, str):
            return COMPARE_INVALID_STEAM_MESSAGE
        steam_ids.append(result)

    if steam_ids[0] == steam_ids[1]:
        return COMPARE_SAME_PLAYER_MESSAGE

    return steam_ids[0], steam_ids[1]


def _split_comparison_result(
    result: Any,
) -> tuple[PlayerStats | None, BaseException | None]:
    """Разбирает результат одного плеча gather для сравнения."""
    if isinstance(result, BaseException):
        return None, result
    if isinstance(result, PlayerStats):
        return result, None
    return None, None


async def _load_comparison_stats(
    left_steam64: str,
    right_steam64: str,
    faceit_client: FaceitClient,
) -> tuple[
    PlayerStats | None,
    PlayerStats | None,
    BaseException | None,
    BaseException | None,
]:
    """Параллельно загружает статистику обоих игроков.

    Внешний gather запускает два независимых сценария ``_load_player_stats``.
    Если один игрок не найден или упал, второй продолжает загрузку.
    """
    results = await asyncio.gather(
        _load_player_stats(left_steam64, faceit_client),
        _load_player_stats(right_steam64, faceit_client),
        return_exceptions=True,
    )

    left_stats, left_error = _split_comparison_result(results[0])
    right_stats, right_error = _split_comparison_result(results[1])

    if left_error is not None:
        logger.warning("Сравнение, ошибка первого игрока: %s", left_error)
    if right_error is not None:
        logger.warning("Сравнение, ошибка второго игрока: %s", right_error)

    return left_stats, right_stats, left_error, right_error


def _comparison_side_error(
    side: int,
    error: BaseException | None,
) -> str:
    """Подбирает сообщение об ошибке для одной стороны сравнения."""
    if error is None:
        if side == 1:
            return COMPARE_FIRST_NOT_FOUND_MESSAGE
        return COMPARE_SECOND_NOT_FOUND_MESSAGE

    if isinstance(error, FaceitRateLimit):
        return FACEIT_RATE_LIMIT_MESSAGE

    if side == 1:
        return COMPARE_FIRST_ERROR_MESSAGE
    return COMPARE_SECOND_ERROR_MESSAGE


async def _build_comparison_message(
    left_steam64: str,
    right_steam64: str,
    faceit_client: FaceitClient,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Загружает двух игроков и формирует текст сравнения."""
    (
        left_stats,
        right_stats,
        left_error,
        right_error,
    ) = await _load_comparison_stats(
        left_steam64,
        right_steam64,
        faceit_client,
    )

    if left_stats is None or right_stats is None:
        if left_stats is None and right_stats is None:
            if left_error is None and right_error is None:
                return COMPARE_BOTH_NOT_FOUND_MESSAGE, None
            if isinstance(left_error, FaceitRateLimit) or isinstance(
                right_error,
                FaceitRateLimit,
            ):
                return FACEIT_RATE_LIMIT_MESSAGE, None
            return COMPARE_BOTH_ERROR_MESSAGE, None

        if left_stats is None:
            return _comparison_side_error(1, left_error), None
        return _comparison_side_error(2, right_error), None

    if left_stats.profile.player_id == right_stats.profile.player_id:
        return COMPARE_SAME_PLAYER_MESSAGE, None

    comparison = build_player_comparison(left_stats, right_stats)
    if not comparison.has_data:
        return COMPARE_NO_DATA_MESSAGE, None

    text = format_player_comparison(comparison)
    keyboard = build_comparison_keyboard(
        comparison,
        refresh_callback_data=(
            f"{REFRESH_COMPARE_CALLBACK_PREFIX}"
            f"{left_steam64}:{right_steam64}"
        ),
    )
    return text, keyboard


async def _handle_compare_request(
    message: Message,
    inputs: list[str],
    faceit_client: FaceitClient,
) -> None:
    """Валидирует ввод и отправляет сравнение двух игроков."""
    await _send_typing(message)

    resolved = await _resolve_compare_inputs(inputs)
    if isinstance(resolved, str):
        await message.answer(resolved)
        return

    left_steam64, right_steam64 = resolved

    try:
        text, keyboard = await _build_comparison_message(
            left_steam64,
            right_steam64,
            faceit_client,
        )
    except FaceitError as exc:
        logger.warning("Не удалось сравнить игроков: %s", exc)
        await message.answer(_error_message(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка при сравнении игроков")
        await message.answer(FACEIT_ERROR_MESSAGE)
        return

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
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


@router.message(Command("compare"))
async def handle_compare(
    message: Message,
    command: CommandObject,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обрабатывает команду /compare с двумя Steam-идентификаторами."""
    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    inputs = _parse_compare_inputs(command.args or "")
    if not inputs:
        await message.answer(COMPARE_USAGE_MESSAGE)
        return
    if len(inputs) == 1:
        await message.answer(COMPARE_ONE_LINK_MESSAGE)
        return
    if len(inputs) > 2:
        await message.answer(COMPARE_TOO_MANY_LINKS_MESSAGE)
        return

    await _handle_compare_request(message, inputs, faceit_client)


@router.message(F.text)
async def handle_link(
    message: Message,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обрабатывает ссылку на Steam-профиль или две ссылки для сравнения."""
    urls = _extract_urls(message.text or "")
    if not urls:
        await message.answer(NO_LINK_MESSAGE)
        return

    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    if len(urls) == 2:
        await _handle_compare_request(message, urls, faceit_client)
        return

    if len(urls) > 2:
        await message.answer(COMPARE_TOO_MANY_LINKS_MESSAGE)
        return

    await _send_typing(message)

    steam64 = await resolve_steam_id(urls[0])
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


@router.callback_query(
    F.data.startswith(REFRESH_COMPARE_CALLBACK_PREFIX)
)
async def handle_compare_refresh(
    query: CallbackQuery,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обновляет сравнение двух игроков по inline-кнопке."""
    if faceit_client is None:
        await query.answer(
            FACEIT_NOT_CONFIGURED_MESSAGE,
            show_alert=True,
        )
        return

    raw_data = (query.data or "").removeprefix(
        REFRESH_COMPARE_CALLBACK_PREFIX
    )
    left_steam64, separator, right_steam64 = raw_data.partition(":")
    if (
        not separator
        or not STEAM_ID64_RE.fullmatch(left_steam64)
        or not STEAM_ID64_RE.fullmatch(right_steam64)
    ):
        await query.answer(
            "Некорректные данные для обновления.",
            show_alert=True,
        )
        return

    if not isinstance(query.message, Message):
        await query.answer(
            "Сообщение устарело. Пришли две ссылки ещё раз.",
            show_alert=True,
        )
        return

    await query.answer("Обновляю сравнение…")
    await _send_typing(query.message)

    faceit_client.clear_cache()
    try:
        text, keyboard = await _build_comparison_message(
            left_steam64,
            right_steam64,
            faceit_client,
        )
    except FaceitError as exc:
        logger.warning("Не удалось обновить сравнение: %s", exc)
        await query.message.answer(_error_message(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка при обновлении сравнения")
        await query.message.answer(FACEIT_ERROR_MESSAGE)
        return

    try:
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            logger.warning("Не удалось обновить сообщение сравнения: %s", exc)
            await query.message.answer(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
