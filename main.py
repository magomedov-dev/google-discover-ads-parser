"""Точка входа: запуск парсера рекламы Google Discover на устройствах флота.

Сценарий и вся логика — в пакете :mod:`googleadsparser`. Здесь только настройка
логирования и запуск через :class:`~axonctl.FleetController` над устройствами из
``fleet.toml``.
"""

import asyncio
import logging

from axonctl import FleetController

from googleadsparser import google_parser

logger = logging.getLogger("googleadsparser")


async def main() -> None:
    """Настроить логирование и запустить парсер на устройствах флота."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    async with FleetController.from_config("fleet.toml") as fleet:
        results = await fleet.run(google_parser, targets="demo")

    for serial, outcome in results.items():
        if outcome.ok:
            logger.info("[%s] готово", serial)
        else:
            logger.error("[%s] прогон упал: %s", serial, outcome.error)


if __name__ == "__main__":
    asyncio.run(main())
