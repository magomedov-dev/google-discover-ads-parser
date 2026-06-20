"""Парсер рекламы из ленты Google Discover (приложение Google для Android).

Публичный API пакета:

* :class:`~googleadsparser.scraper.GoogleParser` — скрейпер на одно устройство;
* :func:`~googleadsparser.scraper.google_parser` — сценарий для ``FleetController.run``;
* :class:`~googleadsparser.models.AdInfo` — разобранные данные рекламы;
* :class:`~googleadsparser.selectors.GoogleSelectors` — селекторы UI.
"""

from .models import AdInfo
from .scraper import GoogleParser, google_parser
from .selectors import GoogleSelectors

__all__ = ["AdInfo", "GoogleParser", "GoogleSelectors", "google_parser"]
