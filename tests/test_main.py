from __future__ import annotations

import logging

import pytest
from aiogram import Router

from bot.rate_limit import (
    RateLimitMiddleware,
    RefreshCooldown,
    SlidingWindowRateLimiter,
)


def test_create_dispatcher_registers_rate_limit_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "42:TEST")

    # Импорт после установки BOT_TOKEN: настройки не должны требовать .env
    # в тестовом окружении.
    import bot.main as main_module

    # Свежий роутер, чтобы не привязывать глобальный handlers.router
    # к второму Dispatcher (aiogram это запрещает).
    monkeypatch.setattr(main_module, "router", Router())

    limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60)
    cooldown = RefreshCooldown(60)

    with caplog.at_level(logging.WARNING, logger=main_module.logger.name):
        dispatcher = main_module.create_dispatcher(
            None,
            rate_limiter=limiter,
            refresh_cooldown=cooldown,
        )

    assert len(dispatcher.message.outer_middleware) == 1
    assert len(dispatcher.callback_query.outer_middleware) == 1
    assert isinstance(
        dispatcher.message.outer_middleware[0],
        RateLimitMiddleware,
    )
    assert isinstance(
        dispatcher.callback_query.outer_middleware[0],
        RateLimitMiddleware,
    )
    middleware = dispatcher.message.outer_middleware[0]
    assert isinstance(middleware, RateLimitMiddleware)
    assert middleware._limiter is limiter
    assert dispatcher["faceit_client"] is None
    assert dispatcher["refresh_cooldown"] is cooldown
    assert any(
        "faceit_client не передан" in record.getMessage()
        for record in caplog.records
    )
