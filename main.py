"""CLI-точка входа: запуск парсера рекламы Google Discover на устройствах.

Параметры берутся из TOML-конфига (``config.toml`` по умолчанию) и/или флагов CLI;
флаги имеют приоритет над конфигом. Флот собирается программно — отдельный
``fleet.toml`` не нужен: устройства задаются ``--device`` / секцией ``[fleet]`` в
конфиге, а при их отсутствии определяются автоматически через ``adb devices``.

Примеры::

    uv run main.py                          # все подключённые устройства, config.toml
    uv run main.py --device 276bcca9        # конкретное устройство
    uv run main.py --swipes 40 --concurrency 4
    uv run main.py --config my.toml -o ads2
    uv run main.py --list-devices           # показать подключённые устройства
    uv run main.py --dump                   # дебаг: дамп UI текущего экрана в ui.html
    uv run main.py --dump screen.html -d 276bcca9
"""

import argparse
import asyncio
import logging
import subprocess
from dataclasses import replace
from pathlib import Path

from axonctl import Device, FleetConfig, FleetController

from googleadsparser import google_parser
from googleadsparser.config import FleetOptions, ScrapeConfig, load_config
from googleadsparser.debug import dump

logger = logging.getLogger("googleadsparser")


def detect_devices() -> list[str]:
    """Серийники подключённых устройств по выводу ``adb devices``.

    Returns:
        Список серийников в состоянии ``device`` (без offline/unauthorized).
    """
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True).stdout
    devices: list[str] = []
    for line in out.splitlines()[1:]:  # первая строка — заголовок «List of devices…»
        parts = line.split()
        if parts and parts[-1] == "device":  # serial \t device (не offline/unauthorized)
            devices.append(parts[0])
    return devices


def build_arg_parser() -> argparse.ArgumentParser:
    """Собрать парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="googleadsparser",
        description="Парсер рекламы из ленты Google Discover (Android).",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="путь к TOML-конфигу (по умолчанию config.toml; если нет — дефолты)",
    )
    parser.add_argument(
        "-d",
        "--device",
        action="append",
        dest="devices",
        metavar="SERIAL",
        help="серийник устройства (можно несколько); иначе — из конфига или adb",
    )
    parser.add_argument("--concurrency", type=int, help="лимит одновременных устройств")
    parser.add_argument("--swipes", type=int, help="свайпов за одну обработку ленты")
    parser.add_argument("-o", "--output-dir", type=Path, help="каталог для собранной рекламы")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="уровень логирования (DEBUG/INFO/WARNING/ERROR; по умолчанию INFO)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="показать подключённые устройства и выйти",
    )
    parser.add_argument(
        "--dump",
        nargs="?",
        const="ui.html",
        metavar="PATH",
        help="дебаг-режим: снять дамп UI текущего экрана (device.inspect) и выйти; "
        "по умолчанию в ui.html (для нескольких устройств серийник добавляется в имя)",
    )
    return parser


def resolve_devices(cli_devices: list[str] | None, fleet: FleetOptions) -> list[str]:
    """Определить список устройств: CLI → конфиг → автоопределение через adb.

    Args:
        cli_devices: Серийники из аргументов CLI (или ``None``).
        fleet: Опции флота из конфига (могут содержать список устройств).

    Returns:
        Итоговый список серийников (возможно пустой).
    """
    if cli_devices:
        return cli_devices
    if fleet.devices:
        return list(fleet.devices)
    return detect_devices()


async def dump_ui(devices: list[str], path: str) -> None:
    """Дебаг-режим: снять дамп UI текущего экрана устройств (без запуска парсера).

    Для одного устройства дамп пишется в ``path``; для нескольких к имени файла
    добавляется серийник, чтобы файлы не перетирались.

    Args:
        devices: Серийники устройств.
        path: Базовый путь HTML-дампа.
    """
    fleet_config = FleetConfig(devices={serial: frozenset() for serial in devices})

    async def scenario(device: Device) -> None:
        out = Path(path)
        if len(devices) > 1:
            out = out.with_name(f"{out.stem}_{device.serial}{out.suffix}")
        await dump(device, str(out))

    async with FleetController(fleet_config) as fleet:
        await fleet.run(scenario)


async def run(scrape: ScrapeConfig, concurrency: int, devices: list[str]) -> None:
    """Запустить парсер на указанных устройствах.

    Args:
        scrape: Параметры скрейпинга.
        concurrency: Лимит одновременных сценариев.
        devices: Серийники устройств.
    """
    fleet_config = FleetConfig(
        concurrency=concurrency,
        devices={serial: frozenset() for serial in devices},
    )
    async with FleetController(fleet_config) as fleet:
        results = await fleet.run(lambda device: google_parser(device, scrape))

    for serial, outcome in results.items():
        if outcome.ok:
            logger.info("[%s] готово", serial)
        else:
            logger.error("[%s] прогон упал: %s", serial, outcome.error)


def main() -> None:
    """Разобрать аргументы, загрузить конфиг и запустить парсер."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_devices:
        for serial in detect_devices():
            print(serial)
        return

    scrape, fleet = load_config(args.config)

    # Флаги CLI переопределяют конфиг.
    if args.swipes is not None:
        scrape = replace(scrape, swipes=args.swipes)
    if args.output_dir is not None:
        scrape = replace(scrape, output_dir=args.output_dir)
    if args.concurrency is not None:
        fleet = replace(fleet, concurrency=args.concurrency)

    devices = resolve_devices(args.devices, fleet)
    if not devices:
        raise SystemExit("нет устройств: укажи --device или подключи устройство по adb")

    # Дебаг-режим: снять дамп UI и выйти, парсер не запускаем.
    if args.dump is not None:
        logger.info("дебаг-дамп UI: %s", ", ".join(devices))
        asyncio.run(dump_ui(devices, args.dump))
        return

    logger.info("устройства: %s | свайпов: %d", ", ".join(devices), scrape.swipes)
    asyncio.run(run(scrape, fleet.concurrency, devices))


if __name__ == "__main__":
    main()
