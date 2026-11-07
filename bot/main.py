from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.faceit import FaceitClient
from bot.handlers import router
from config import settings

logger = logging.getLogger(__name__)


def create_bot() -> Bot:
    """Создаёт экземпляр бота с HTML-разметкой по умолчанию."""
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(
    faceit_client: FaceitClient | None = None,
) -> Dispatcher:
    """Создаёт диспетчер, подключает роутеры и зависимости."""
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["faceit_client"] = faceit_client
    return dispatcher


async def main() -> None:
    """Запускает бота в режиме long polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    if settings.steam_api_key is None:
        logger.warning(
            "STEAM_API_KEY не задан — короткие Steam-ссылки работать не будут"
        )

    faceit_client: FaceitClient | None = None
    if settings.faceit_api_key is None:
        logger.warning(
            "FACEIT_API_KEY не задан — запросы статистики Faceit недоступны"
        )
    else:
        faceit_client = FaceitClient(
            settings.faceit_api_key.get_secret_value()
        )

    bot = create_bot()
    dispatcher = create_dispatcher(faceit_client)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен в режиме long polling")
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        if faceit_client is not None:
            await faceit_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Бот остановлен")
