"""Отладочные утилиты: дамп UI-структуры узла в JSON и интерактивного HTML.

Временные помощники для подбора селекторов и анализа структуры рекламных карточек.

TODO: удалить, когда разбор карточек будет стабилизирован.
"""

import json
import logging
from pathlib import Path

from axonctl import Device, UiNode

logger = logging.getLogger("googleadsparser")


def _node_to_dict(node: UiNode) -> dict:
    """Рекурсивно превратить узел и его потомков в JSON-совместимый словарь.

    Args:
        node: Узел, чьё поддерево сериализуется.

    Returns:
        Словарь с полями узла и вложенным списком ``children``.
    """
    bounds = None
    if node.bounds is not None:
        bounds = {
            "left": node.bounds.left,
            "top": node.bounds.top,
            "right": node.bounds.right,
            "bottom": node.bounds.bottom,
        }
    center = None
    if node.center is not None:
        center = {"x": node.center.x, "y": node.center.y}
    return {
        "node_id": node.node_id,
        "class_name": node.class_name,
        "text": node.text,
        "resource_id": node.resource_id,
        "content_desc": node.content_desc,
        "clickable": node.clickable,
        "enabled": node.enabled,
        "focused": node.focused,
        "bounds": bounds,
        "center": center,
        "children": [_node_to_dict(child) for child in node.children],
    }


def save_node(node: UiNode, path: Path) -> None:
    """Сохранить UI-поддерево узла в JSON-файл.

    Args:
        node: Узел, чьё поддерево сериализуется (вместе со всеми потомками).
        path: Путь к итоговому ``.json``-файлу; родительские каталоги создаются.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_node_to_dict(node), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("структура узла сохранена в %s", path)


async def dump(device: Device, path: str = "ui.html") -> None:
    """Сохранить интерактивный снимок текущего UI в HTML.

    Открыв полученный файл в браузере, можно осмотреть дерево элементов, их
    ``resource_id``/``text``/границы и подобрать селекторы.

    Args:
        device: Устройство, с которого снимаем дамп.
        path: Куда сохранить HTML-инспектор.
    """
    await device.inspect(path)
    logger.info("[%s] UI-дамп сохранён в %s", device.serial, path)
