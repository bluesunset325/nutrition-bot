"""Сетевые настройки: обход проблемы с DNS-резолвером aiodns.

Если в окружении установлен пакет `aiodns`, aiohttp по умолчанию берёт
AsyncResolver (c-ares). На части систем — корпоративный DNS, VPN, DNS-over-HTTPS,
нестандартный /etc/resolv.conf — c-ares не может достучаться до серверов и
падает с «Could not contact DNS servers», хотя обычный системный резолвер
(getaddrinfo) те же имена разрешает без проблем.

Здесь всё принудительно переводится на ThreadedResolver, который использует
системный getaddrinfo. Отключается через USE_THREADED_RESOLVER=false в .env.
"""

from __future__ import annotations

from typing import Any, Optional

from aiohttp import ThreadedResolver
from aiohttp.abc import AbstractResolver
from aiogram.client.session.aiohttp import AiohttpSession


def create_resolver() -> AbstractResolver:
    return ThreadedResolver()


class SystemDnsAiohttpSession(AiohttpSession):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # _connector_init — словарь аргументов для TCPConnector внутри aiogram;
        # добавляем свой резолвер, сохраняя ssl/limit/ttl_dns_cache от aiogram.
        self._connector_init["resolver"] = create_resolver()


def create_bot_session(use_threaded_resolver: bool) -> Optional[AiohttpSession]:
    if not use_threaded_resolver:
        return None
    return SystemDnsAiohttpSession()
