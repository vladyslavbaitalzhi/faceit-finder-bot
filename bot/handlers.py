from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Mapping
from html import escape as escape_html
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.faceit import FaceitClient, FaceitError, FaceitNotFound, FaceitRateLimit
from bot.formatter import (
    CANCEL_TEXTS,
    MENU_CANCEL_CALLBACK,
    MENU_COMPARE_CALLBACK,
    MENU_COMPARE_TEXT,
    MENU_FIND_CALLBACK,
    MENU_FIND_TEXT,
    MENU_HELP_CALLBACK,
    MENU_HELP_TEXT,
    MENU_MAIN_CALLBACK,
    build_cancel_keyboard,
    build_comparison_keyboard,
    build_help_keyboard,
    build_main_menu_keyboard,
    build_player_keyboard,
    build_start_keyboard,
    format_player_comparison,
    format_player_stats,
)
from bot.rate_limit import RefreshCooldown
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


class CompareFlow(StatesGroup):
    """Состояния пошагового сравнения двух игроков."""

    waiting_first = State()
    waiting_second = State()


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
REFRESH_CALLBACK_PREFIX = "refresh:"
REFRESH_COMPARE_CALLBACK_PREFIX = "refresh_compare:"
COMPARE_SEPARATORS = {"vs", "vs.", "и", "and", "&", "против"}
COMPARE_LEFT_STEAM64_KEY = "compare_left_steam64"

START_MESSAGE = (
    "<b>Привет!</b> 👋\n\n"
    "Я показываю статистику CS2 на <b>Faceit</b>.\n\n"
    "Выбери действие кнопками ниже или просто отправь ссылку "
    "на Steam-профиль.\n\n"
    "Например: <code>https://steamcommunity.com/id/username</code>"
)

MENU_HINT_MESSAGE = "Панель управления всегда внизу 👇"

HELP_MESSAGE = (
    "<b>Как пользоваться ботом</b>\n\n"
    "1. Нажми «🔍 Найти игрока» или отправь ссылку на Steam-профиль, "
    "например:\n"
    "<code>https://steamcommunity.com/id/username</code>\n"
    "2. Бот найдёт игрока на Faceit и покажет статистику CS2.\n\n"
    "Для сравнения нажми «⚔️ Сравнить игроков» — бот попросит ссылки "
    "по очереди. Можно также отправить две ссылки одним сообщением "
    "или использовать:\n"
    "<code>/compare url1 url2</code>\n\n"
    "Отменить пошаговый ввод можно кнопкой «Отмена» или командой "
    "<code>/cancel</code>.\n\n"
    "<b>Команды</b>\n"
    "/start — показать главное меню\n"
    "/help — показать эту справку\n"
    "/compare url1 url2 — сравнить двух игроков\n"
    "/cancel — отменить пошаговый ввод"
)

FIND_PROMPT_MESSAGE = (
    "<b>🔍 Поиск игрока</b>\n\n"
    "Отправь ссылку на Steam-профиль, SteamID64 или vanity-имя.\n\n"
    "Например: <code>https://steamcommunity.com/id/username</code>"
)

COMPARE_FIRST_PROMPT_MESSAGE = (
    "<b>⚔️ Сравнение игроков</b>\n\n"
    "Шаг 1 из 2. Отправь ссылку на <b>первого</b> игрока.\n"
    "Можно также отправить сразу две ссылки одним сообщением."
)

COMPARE_SECOND_PROMPT_MESSAGE = (
    "<b>⚔️ Сравнение игроков</b>\n\n"
    "Первый игрок: <b>{nickname}</b>\n"
    "Шаг 2 из 2. Теперь отправь ссылку на <b>второго</b> игрока."
)

CANCELLED_MESSAGE = (
    "<b>Действие отменено.</b>\n\n"
    "Выбери действие на панели ниже или начни заново."
)

FLOW_NO_LINK_MESSAGE = (
    "Не вижу ссылки. Отправь ссылку на Steam-профиль "
    "или нажми «Отмена»."
)

FLOW_ONE_LINK_MESSAGE = (
    "Для этого шага нужна одна ссылка. "
    "Отправь её или нажми «Отмена»."
)

FLOW_INVALID_STEAM_MESSAGE = (
    "Не удалось получить SteamID64. "
    "Проверь ссылку и попробуй ещё раз."
)

FLOW_EXPIRED_MESSAGE = (
    "Сессия сравнения устарела. Начни сравнение заново."
)

REFRESH_FRESH_MESSAGE = (
    "Данные уже свежие, обновлены {seconds} сек назад."
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


def _is_cancel_text(text: str) -> bool:
    """Проверяет, что пользователь просит отменить пошаговый сценарий."""
    return text.strip().casefold() in CANCEL_TEXTS


async def _refresh_is_allowed(
    query: CallbackQuery,
    targets: list[str],
    refresh_cooldown: RefreshCooldown | None,
) -> bool:
    """Проверяет cooldown refresh и отвечает на callback, если он активен."""
    if refresh_cooldown is None:
        return True

    status = refresh_cooldown.status(targets)
    if not status.blocked:
        return True

    await query.answer(
        REFRESH_FRESH_MESSAGE.format(
            seconds=max(0, int(status.elapsed)),
        ),
        show_alert=False,
    )
    return False


async def _edit_inline_or_answer(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Редактирует сообщение с inline-клавиатурой или отправляет новое."""
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        logger.warning("Не удалось отредактировать меню: %s", exc)
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
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
        "lifetime": faceit_client.get_lifetime_stats(
            player_id,
            steam64=steam64,
        ),
        "recent": faceit_client.get_recent_matches(
            player_id,
            limit=RECENT_MATCHES_FETCH_LIMIT,
            steam64=steam64,
        ),
        "steam": fetch_steam_stats(steam64),
    }
    if isinstance(region, str) and region:
        requests["region_ranking"] = faceit_client.get_player_ranking(
            player_id,
            region,
            steam64=steam64,
        )
        if isinstance(country, str) and country:
            requests["country_ranking"] = faceit_client.get_player_ranking(
                player_id,
                region,
                country=country,
                steam64=steam64,
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
        return _rate_limit_message(error)

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
            rate_limit_error = (
                left_error
                if isinstance(left_error, FaceitRateLimit)
                else right_error
            )
            if isinstance(rate_limit_error, FaceitRateLimit):
                return _rate_limit_message(rate_limit_error), None
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


async def _send_comparison_result(
    message: Message,
    left_steam64: str,
    right_steam64: str,
    faceit_client: FaceitClient,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Загружает двух игроков и отправляет готовое сравнение."""
    await _send_typing(message)
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

    if keyboard is not None and refresh_cooldown is not None:
        refresh_cooldown.mark([left_steam64, right_steam64])


async def _handle_compare_request(
    message: Message,
    inputs: list[str],
    faceit_client: FaceitClient,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Валидирует ввод и отправляет сравнение двух игроков."""
    resolved = await _resolve_compare_inputs(inputs)
    if isinstance(resolved, str):
        await message.answer(resolved)
        return

    left_steam64, right_steam64 = resolved
    await _send_comparison_result(
        message,
        left_steam64,
        right_steam64,
        faceit_client,
        refresh_cooldown,
    )


async def _start_compare_flow(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient | None,
) -> None:
    """Запускает пошаговый сценарий сравнения."""
    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    await state.clear()
    await state.set_state(CompareFlow.waiting_first)
    await message.answer(
        COMPARE_FIRST_PROMPT_MESSAGE,
        reply_markup=build_cancel_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _cancel_compare_flow(
    message: Message,
    state: FSMContext,
) -> None:
    """Сбрасывает состояние и показывает экран отмены."""
    await state.clear()
    await message.answer(
        CANCELLED_MESSAGE,
        reply_markup=build_start_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _handle_compare_first_input(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Обрабатывает первый шаг пошагового сравнения."""
    inputs = _parse_compare_inputs(message.text or "")
    if not inputs:
        await message.answer(FLOW_NO_LINK_MESSAGE)
        return
    if len(inputs) > 2:
        await message.answer(COMPARE_TOO_MANY_LINKS_MESSAGE)
        return
    if len(inputs) == 2:
        await state.clear()
        await _handle_compare_request(
            message,
            inputs,
            faceit_client,
            refresh_cooldown,
        )
        return

    steam64 = await resolve_steam_id(inputs[0])
    if steam64 is None:
        await message.answer(FLOW_INVALID_STEAM_MESSAGE)
        return

    try:
        player = await faceit_client.get_player_by_steam_id(steam64)
    except FaceitError as exc:
        logger.warning("Сравнение, ошибка первого игрока: %s", exc)
        await message.answer(_error_message(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка на первом шаге сравнения")
        await message.answer(FACEIT_ERROR_MESSAGE)
        return

    if player is None:
        await message.answer(COMPARE_FIRST_NOT_FOUND_MESSAGE)
        return

    nickname = str(player.get("nickname") or steam64)
    await state.update_data(
        **{COMPARE_LEFT_STEAM64_KEY: steam64}
    )
    await state.set_state(CompareFlow.waiting_second)
    await message.answer(
        COMPARE_SECOND_PROMPT_MESSAGE.format(
            nickname=escape_html(nickname),
        ),
        reply_markup=build_cancel_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _handle_compare_second_input(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Обрабатывает второй шаг пошагового сравнения."""
    data = await state.get_data()
    left_steam64 = data.get(COMPARE_LEFT_STEAM64_KEY)
    if not isinstance(left_steam64, str) or not STEAM_ID64_RE.fullmatch(
        left_steam64
    ):
        await state.clear()
        await message.answer(
            FLOW_EXPIRED_MESSAGE,
            reply_markup=build_start_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    inputs = _parse_compare_inputs(message.text or "")
    if not inputs:
        await message.answer(FLOW_NO_LINK_MESSAGE)
        return
    if len(inputs) != 1:
        await message.answer(FLOW_ONE_LINK_MESSAGE)
        return

    right_steam64 = await resolve_steam_id(inputs[0])
    if right_steam64 is None:
        await message.answer(FLOW_INVALID_STEAM_MESSAGE)
        return

    if right_steam64 == left_steam64:
        await message.answer(COMPARE_SAME_PLAYER_MESSAGE)
        return

    await state.clear()
    await _send_comparison_result(
        message,
        left_steam64,
        right_steam64,
        faceit_client,
        refresh_cooldown,
    )


def _rate_limit_message(error: FaceitRateLimit) -> str:
    """Добавляет к сообщению о 429 известное время ожидания."""
    if error.retry_after is None:
        return FACEIT_RATE_LIMIT_MESSAGE
    seconds = max(1, math.ceil(error.retry_after))
    return (
        f"{FACEIT_RATE_LIMIT_MESSAGE} "
        f"Повтори примерно через {seconds} сек."
    )


def _error_message(error: Exception) -> str:
    """Подбирает понятный пользователю текст ошибки Faceit."""
    if isinstance(error, FaceitRateLimit):
        return _rate_limit_message(error)
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
async def handle_start(
    message: Message,
    state: FSMContext,
) -> None:
    """Обрабатывает команду /start и показывает главное меню."""
    await state.clear()
    await message.answer(
        START_MESSAGE,
        reply_markup=build_start_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await message.answer(
        MENU_HINT_MESSAGE,
        reply_markup=build_main_menu_keyboard(),
    )


@router.message(Command("help"))
async def handle_help(
    message: Message,
    state: FSMContext,
) -> None:
    """Обрабатывает команду /help."""
    await state.clear()
    await message.answer(
        HELP_MESSAGE,
        reply_markup=build_help_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(Command("cancel"))
async def handle_cancel(
    message: Message,
    state: FSMContext,
) -> None:
    """Отменяет пошаговый сценарий и показывает меню."""
    await state.clear()
    await message.answer(
        CANCELLED_MESSAGE,
        reply_markup=build_start_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(Command("compare"))
async def handle_compare(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Обрабатывает команду /compare с двумя Steam-идентификаторами."""
    await state.clear()

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

    await _handle_compare_request(
        message,
        inputs,
        faceit_client,
        refresh_cooldown,
    )


@router.message(F.text == MENU_FIND_TEXT)
async def handle_menu_find_text(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обрабатывает кнопку Reply-меню «Найти игрока»."""
    await state.clear()
    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    await message.answer(
        FIND_PROMPT_MESSAGE,
        reply_markup=build_cancel_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(F.text == MENU_COMPARE_TEXT)
async def handle_menu_compare_text(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Обрабатывает кнопку Reply-меню «Сравнить игроков»."""
    await _start_compare_flow(message, state, faceit_client)


@router.message(F.text == MENU_HELP_TEXT)
async def handle_menu_help_text(
    message: Message,
    state: FSMContext,
) -> None:
    """Обрабатывает кнопку Reply-меню «Помощь»."""
    await state.clear()
    await message.answer(
        HELP_MESSAGE,
        reply_markup=build_help_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(CompareFlow.waiting_first, F.text)
async def handle_compare_first_step(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Принимает первого игрока в пошаговом сравнении."""
    if _is_cancel_text(message.text or ""):
        await _cancel_compare_flow(message, state)
        return

    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    await _handle_compare_first_input(
        message,
        state,
        faceit_client,
        refresh_cooldown,
    )


@router.message(CompareFlow.waiting_second, F.text)
async def handle_compare_second_step(
    message: Message,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
    refresh_cooldown: RefreshCooldown | None = None,
) -> None:
    """Принимает второго игрока и отправляет сравнение."""
    if _is_cancel_text(message.text or ""):
        await _cancel_compare_flow(message, state)
        return

    if faceit_client is None:
        await message.answer(FACEIT_NOT_CONFIGURED_MESSAGE)
        return

    await _handle_compare_second_input(
        message,
        state,
        faceit_client,
        refresh_cooldown,
    )


@router.message(F.text)
async def handle_link(
    message: Message,
    faceit_client: FaceitClient | None = None,
    refresh_cooldown: RefreshCooldown | None = None,
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
        await _handle_compare_request(
            message,
            urls,
            faceit_client,
            refresh_cooldown,
        )
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

    if refresh_cooldown is not None:
        refresh_cooldown.mark([steam64])


@router.callback_query(F.data == MENU_FIND_CALLBACK)
async def handle_menu_find(
    query: CallbackQuery,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Открывает экран поиска игрока из inline-меню."""
    if faceit_client is None:
        await query.answer(
            FACEIT_NOT_CONFIGURED_MESSAGE,
            show_alert=True,
        )
        return

    await state.clear()
    await query.answer()

    if isinstance(query.message, Message):
        await _edit_inline_or_answer(
            query.message,
            FIND_PROMPT_MESSAGE,
            reply_markup=build_cancel_keyboard(),
        )


@router.callback_query(F.data == MENU_COMPARE_CALLBACK)
async def handle_menu_compare(
    query: CallbackQuery,
    state: FSMContext,
    faceit_client: FaceitClient | None = None,
) -> None:
    """Запускает пошаговое сравнение из inline-меню."""
    if faceit_client is None:
        await query.answer(
            FACEIT_NOT_CONFIGURED_MESSAGE,
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(CompareFlow.waiting_first)
    await query.answer()

    if isinstance(query.message, Message):
        await _edit_inline_or_answer(
            query.message,
            COMPARE_FIRST_PROMPT_MESSAGE,
            reply_markup=build_cancel_keyboard(),
        )


@router.callback_query(F.data == MENU_HELP_CALLBACK)
async def handle_menu_help(
    query: CallbackQuery,
    state: FSMContext,
) -> None:
    """Открывает справку из inline-меню."""
    await state.clear()
    await query.answer()

    if isinstance(query.message, Message):
        await _edit_inline_or_answer(
            query.message,
            HELP_MESSAGE,
            reply_markup=build_help_keyboard(),
        )


@router.callback_query(F.data == MENU_CANCEL_CALLBACK)
async def handle_menu_cancel(
    query: CallbackQuery,
    state: FSMContext,
) -> None:
    """Отменяет пошаговый сценарий по inline-кнопке."""
    await state.clear()
    await query.answer("Отменено")

    if isinstance(query.message, Message):
        await _edit_inline_or_answer(
            query.message,
            CANCELLED_MESSAGE,
            reply_markup=build_start_keyboard(),
        )


@router.callback_query(F.data == MENU_MAIN_CALLBACK)
async def handle_menu_main(
    query: CallbackQuery,
    state: FSMContext,
) -> None:
    """Возвращает пользователя на стартовый экран."""
    await state.clear()
    await query.answer()

    if isinstance(query.message, Message):
        await _edit_inline_or_answer(
            query.message,
            START_MESSAGE,
            reply_markup=build_start_keyboard(),
        )


@router.callback_query(F.data.startswith(REFRESH_CALLBACK_PREFIX))
async def handle_refresh(
    query: CallbackQuery,
    faceit_client: FaceitClient | None = None,
    refresh_cooldown: RefreshCooldown | None = None,
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

    if not await _refresh_is_allowed(
        query,
        [steam64],
        refresh_cooldown,
    ):
        return

    # Резервируем cooldown сразу после проверки, до первого await:
    # параллельный refresh той же цели увидит отметку и не запустит
    # второй запрос к Faceit.
    if refresh_cooldown is not None:
        refresh_cooldown.mark([steam64])

    await query.answer("Обновляю статистику…")

    if query.bot is not None:
        await query.bot.send_chat_action(
            chat_id=query.message.chat.id,
            action=ChatAction.TYPING,
        )

    faceit_client.invalidate_player_by_steam_id(steam64)
    try:
        stats = await _load_player_stats(steam64, faceit_client)
    except FaceitError as exc:
        if refresh_cooldown is not None:
            refresh_cooldown.release([steam64])
        logger.warning("Не удалось обновить статистику Faceit: %s", exc)
        await query.message.answer(_error_message(exc))
        return
    except Exception:
        if refresh_cooldown is not None:
            refresh_cooldown.release([steam64])
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
    refresh_cooldown: RefreshCooldown | None = None,
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

    targets = [left_steam64, right_steam64]
    if not await _refresh_is_allowed(
        query,
        targets,
        refresh_cooldown,
    ):
        return

    # Резервируем cooldown до первого await, чтобы два параллельных
    # refresh не дублировали запросы к Faceit.
    if refresh_cooldown is not None:
        refresh_cooldown.mark(targets)

    await query.answer("Обновляю сравнение…")
    await _send_typing(query.message)

    faceit_client.invalidate_player_by_steam_id(left_steam64)
    faceit_client.invalidate_player_by_steam_id(right_steam64)
    try:
        text, keyboard = await _build_comparison_message(
            left_steam64,
            right_steam64,
            faceit_client,
        )
    except FaceitError as exc:
        if refresh_cooldown is not None:
            refresh_cooldown.release(targets)
        logger.warning("Не удалось обновить сравнение: %s", exc)
        await query.message.answer(_error_message(exc))
        return
    except Exception:
        if refresh_cooldown is not None:
            refresh_cooldown.release(targets)
        logger.exception("Неожиданная ошибка при обновлении сравнения")
        await query.message.answer(FACEIT_ERROR_MESSAGE)
        return

    if keyboard is None and refresh_cooldown is not None:
        # Сравнение не построено (игрок не найден, 429 и т.п.):
        # не удерживаем cooldown на неудачной загрузке.
        refresh_cooldown.release(targets)

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
