"""Конфигурация парсера: настраиваемые параметры из TOML-файла.

Делится на две части:

* :class:`ScrapeConfig` — параметры самого скрейпинга (свайпы, таймауты, паузы);
* :class:`FleetOptions` — параметры флота устройств (конкуренция, список серийников).

Загружается из TOML вида::

    [scrape]
    swipes = 25
    ready_timeout = 15.0

    [fleet]
    concurrency = 8
    devices = ["276bcca9"]
"""

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


def _kwargs_for(cls: type, section: dict[str, Any]) -> dict[str, Any]:
    """Отобрать из секции TOML только те ключи, что есть полями у dataclass.

    Args:
        cls: Класс-dataclass, для которого собираем аргументы.
        section: Словарь из TOML-секции.

    Returns:
        Словарь ``{поле: значение}`` для конструктора ``cls``.
    """
    known = {f.name for f in fields(cls)}
    return {key: value for key, value in section.items() if key in known}


@dataclass(frozen=True, slots=True)
class ScrapeConfig:
    """Настраиваемые параметры скрейпинга (значения по умолчанию — рабочие).

    Attributes:
        swipes: Сколько свайпов (итераций) делать за одну обработку ленты.
        output_dir: Корневой каталог для собранной рекламы (подкаталог на устройство).
        align_tolerance: Допуск выравнивания низа рекламы (px).
        ready_timeout: Ожидание появления элементов (медленный старт/инет), с.
        ad_load_timeout: Ожидание загрузки сайта/диалогов рекламы, с.
        ad_dwell_min: Минимум «просмотра» сайта рекламы перед закрытием, с.
        ad_dwell_max: Максимум «просмотра» сайта рекламы перед закрытием, с.
        back_attempts: Сколько раз жмём back, возвращаясь в ленту, прежде чем сдаться.
        post_ad_swipes: Сколько свайпов делать после рекламы, чтобы не найти её снова.
        min_media_height: Минимальная высота медиа-области (px), иначе её нет.
        settle: Базовая пауза «дать UI/ленте успокоиться», с.
        settle_timeout: Максимум ожидания, пока лента догрузится и остановится, с.
        settle_stable: Сколько одинаковых дампов подряд считаем «лента успокоилась».
        launch_attempts: Попыток запустить приложение, прежде чем сдаться.
    """

    swipes: int = 25
    output_dir: Path = Path("ads")
    align_tolerance: int = 20
    ready_timeout: float = 15.0
    ad_load_timeout: float = 3.0
    ad_dwell_min: float = 2.0
    ad_dwell_max: float = 5.0
    back_attempts: int = 5
    post_ad_swipes: int = 2
    min_media_height: int = 80
    settle: float = 0.5
    settle_timeout: float = 8.0
    settle_stable: int = 2
    launch_attempts: int = 3

    @classmethod
    def from_section(cls, section: dict[str, Any]) -> "ScrapeConfig":
        """Собрать конфиг из секции ``[scrape]`` TOML (неизвестные ключи игнорируются).

        Args:
            section: Словарь секции ``[scrape]``.

        Returns:
            Конфиг скрейпинга с применёнными значениями (остальные — по умолчанию).
        """
        kwargs = _kwargs_for(cls, section)
        if "output_dir" in kwargs:
            kwargs["output_dir"] = Path(kwargs["output_dir"])
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class FleetOptions:
    """Параметры флота устройств.

    Attributes:
        concurrency: Глобальный лимит одновременных сценариев по флоту.
        devices: Серийники устройств; пустой кортеж — автоопределение через ``adb``.
    """

    concurrency: int = 8
    devices: tuple[str, ...] = ()

    @classmethod
    def from_section(cls, section: dict[str, Any]) -> "FleetOptions":
        """Собрать опции флота из секции ``[fleet]`` TOML.

        Args:
            section: Словарь секции ``[fleet]``.

        Returns:
            Опции флота с применёнными значениями (остальные — по умолчанию).
        """
        kwargs = _kwargs_for(cls, section)
        if "devices" in kwargs:
            kwargs["devices"] = tuple(kwargs["devices"])
        return cls(**kwargs)


def load_config(path: str | Path) -> tuple[ScrapeConfig, FleetOptions]:
    """Загрузить конфиг из TOML-файла; если файла нет — вернуть значения по умолчанию.

    Args:
        path: Путь к TOML-файлу конфигурации.

    Returns:
        Кортеж ``(ScrapeConfig, FleetOptions)``.
    """
    path = Path(path)
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    return (
        ScrapeConfig.from_section(data.get("scrape", {})),
        FleetOptions.from_section(data.get("fleet", {})),
    )
