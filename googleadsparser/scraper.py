"""Скрейпер рекламы из ленты Google Discover (приложение Google для Android).

Сценарий на каждое устройство (:class:`GoogleParser`):

1. Запустить приложение Google и дождаться его на переднем плане.
2. Определить рабочую область ленты (между строкой поиска и низом ленты).
3. Листать ленту медленными свайпами, выискивая карточки с пометкой «Sponsored».
4. Найдя рекламу — подвести её низ к низу рабочей области и обработать: общий скрин,
   скрин медиа, текст и канал, затем открыть (видео — тапом по видео, картинка/карусель
   — по тексту; Google Play пропускаем) и собрать ссылки (сайт, для видео — и видео).
   Всё кладётся в ``adNNN`` со сквозной нумерацией; дубли пропускаются.
5. Терпеть транзиентные сбои (медленный интернет, обрыв связи, устаревшее дерево):
   ожидания адаптивны, ошибки изолированы по итерациям, запуск — с повторами.
"""

import asyncio
import json
import logging
import random
import re
from io import BytesIO
from pathlib import Path

from axonctl import (
    AxonError,
    Bounds,
    Device,
    Selector,
    UiNode,
    UiTree,
    WaitTimeout,
    retry_on_stale,
)
from PIL import Image

from .config import ScrapeConfig
from .debug import save_node
from .models import AdInfo
from .selectors import GoogleSelectors

logger = logging.getLogger("googleadsparser")


class GoogleParser:
    """Скрейпер рекламы из ленты Google Discover на одном устройстве.

    Экземпляр привязан к конкретному устройству (создаётся по одному на устройство,
    чтобы не делить изменяемое состояние между параллельными прогонами).

    Args:
        device: Устройство, на котором работает парсер.
        config: Настраиваемые параметры скрейпинга.

    Attributes:
        device: Управляемое устройство.
        config: Параметры скрейпинга (:class:`~googleadsparser.config.ScrapeConfig`).
        working_bounds: Рабочая область ленты; вычисляется в :meth:`get_working_bounds`.
    """

    #: Пакет приложения Google (его и парсим).
    APP: str = "com.google.android.googlequicksearchbox"
    #: Chrome — открывается при случайном тапе по рекламе; его надо закрывать.
    CHROME_APP: str = "com.android.chrome"
    #: Маркеры видео-рекламы в ``resource_id`` (видео — WebView внутри video_frame).
    VIDEO_ID_MARKERS: tuple[str, ...] = ("video_frame", "duration_badge", "webx_web_view")
    #: Классы видео-плеера в карточке.
    VIDEO_CLASSES: tuple[str, ...] = ("VideoView", "SurfaceView", "TextureView")
    #: Классы контейнера карусели (несколько креативов в одном объявлении).
    CAROUSEL_CLASSES: tuple[str, ...] = ("RecyclerView", "ViewPager")

    def __init__(self, device: Device, config: ScrapeConfig | None = None) -> None:
        """Инициализировать парсер для конкретного устройства.

        Args:
            device: Устройство, на котором будет работать парсер.
            config: Параметры скрейпинга; ``None`` — значения по умолчанию.
        """
        self.device = device
        #: Параметры скрейпинга.
        self.config = config or ScrapeConfig()
        #: Реальная ширина экрана в пикселях; заполняется в :meth:`capture_screen_size`.
        self.screen_width: int
        #: Реальная высота экрана в пикселях; заполняется в :meth:`capture_screen_size`.
        self.screen_height: int
        #: Рабочая область ленты; заполняется в :meth:`get_working_bounds`.
        self.working_bounds: Bounds
        #: Последний снятый UI-дамп; обновляется по ходу листания.
        self._root: UiTree
        #: Каталог собранной рекламы для этого устройства.
        self._out_dir = self.config.output_dir / device.serial
        #: Счётчик сохранённых реклам — продолжается с последней на диске (переживает перезапуск).
        self._ad_count: int = self._last_ad_index()
        #: Уже обработанные рекламы (канал + заголовок) — чтобы не собирать дубли.
        self._seen: set[tuple[str | None, str | None]] = set()

    def _last_ad_index(self) -> int:
        """Найти наибольший уже сохранённый номер рекламы, чтобы продолжить нумерацию.

        Сканирует подкаталоги ``adNNN`` в :attr:`_out_dir`; новые рекламы будут
        нумероваться дальше, не перетирая собранные в прошлых запусках.

        Returns:
            Максимальный найденный номер (0, если каталога/реклам ещё нет).
        """
        if not self._out_dir.exists():
            return 0
        indices = [
            int(path.name[2:])
            for path in self._out_dir.iterdir()
            if path.is_dir() and path.name.startswith("ad") and path.name[2:].isdigit()
        ]
        return max(indices, default=0)

    # --------------------------------------------------------------------- #
    # Жизненный цикл приложения
    # --------------------------------------------------------------------- #

    async def launch(self) -> None:
        """Запустить приложение и дождаться его готовности.

        Ждём не только строку поиска, но и саму ленту: на плохом интернете
        ``RecyclerView`` инфлейтится заметно позже поисковой строки.

        Raises:
            axonctl.WaitTimeout: Если приложение/элементы не появились за
                :attr:`READY_TIMEOUT`.
        """
        logger.info("[%s] запускаю %s", self.device.serial, self.APP)
        await self.device.launch(self.APP)
        await self.device.wait_activity(self.APP, timeout=self.config.ready_timeout)
        await self.device.wait_package(self.APP, timeout=self.config.ready_timeout)
        await self.device.wait_for(GoogleSelectors.SEARCH, timeout=self.config.ready_timeout)
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.config.ready_timeout)
        logger.info("[%s] %s на переднем плане", self.device.serial, self.APP)

    async def is_chrome_open(self) -> bool:
        """Открылся ли случайно Chrome (по его тулбару :attr:`GoogleSelectors.CHROME_TOOL_BAR`)."""
        return await self.device.find(GoogleSelectors.CHROME_TOOL_BAR) is not None

    async def close_chrome(self) -> None:
        """Закрыть Chrome, если он случайно открылся (тап по рекламе и т.п.).

        Без этого приложение Google остаётся за Chrome, и его перезапуск не
        возвращает ленту на передний план. Best-effort: ошибки логируются.
        """
        if not await self.is_chrome_open():
            return
        logger.warning("[%s] открылся Chrome — закрываю", self.device.serial)
        try:
            await self.device.kill(self.CHROME_APP)
            await self.device.wait_gone(
                GoogleSelectors.CHROME_TOOL_BAR, timeout=self.config.ready_timeout
            )
        except AxonError as exc:
            logger.warning("[%s] не удалось закрыть Chrome: %s", self.device.serial, exc)

    async def kill(self) -> None:
        """Принудительно закрыть приложение (best-effort).

        Сначала закрываем случайно открывшийся Chrome — иначе Google остаётся за
        ним и перезапуск не срабатывает. Ошибки закрытия (например, при обрыве
        связи) логируются, но не пробрасываются — иначе замаскировали бы причину сбоя.
        """
        await self.close_chrome()
        try:
            await self.device.kill(self.APP)
            await self.device.wait_gone(GoogleSelectors.ROOT, timeout=self.config.ready_timeout)
            logger.info("[%s] %s закрыт", self.device.serial, self.APP)
        except AxonError as exc:
            logger.warning("[%s] не удалось чисто закрыть приложение: %s", self.device.serial, exc)

    async def capture_screen_size(self) -> None:
        """Снять реальный размер экрана (в пикселях) и сохранить в атрибуты.

        Источник — фактические размеры скриншота: это честное разрешение рендера,
        не зависящее от плотности или вырезов. Результат пишется в
        :attr:`screen_width` и :attr:`screen_height`.
        """
        image = await self.take_screenshot()
        self.screen_width, self.screen_height = image.size
        logger.info(
            "[%s] размер экрана: %dx%d",
            self.device.serial,
            self.screen_width,
            self.screen_height,
        )

    # --------------------------------------------------------------------- #
    # Рабочая область
    # --------------------------------------------------------------------- #

    @retry_on_stale(attempts=3)
    async def get_working_bounds(self) -> Bounds:
        """Определить рабочую область ленты.

        Поднимаем ленту свайпом так, чтобы её верх встал под строкой поиска, и
        берём область от низа строки поиска до низа ленты. Помечено
        :func:`~axonctl.retry_on_stale`: если дерево «уедет» во время чтения,
        шаг повторится.

        Returns:
            Прямоугольник рабочей области (left/top/right/bottom).

        Raises:
            RuntimeError: Если лента или строка поиска не найдены / без границ.
            axonctl.WaitTimeout: Если лента не появилась за :attr:`READY_TIMEOUT`.
        """
        # Дождаться ленты на случай медленной прогрузки после холодного старта.
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.config.ready_timeout)

        root = await self.device.dump()
        feed = root.find(GoogleSelectors.FEED)
        search = root.find(GoogleSelectors.SEARCH)

        if feed is None or feed.bounds is None:
            raise RuntimeError("не найдена лента Discover")
        if search is None or search.bounds is None:
            raise RuntimeError("не найдена строка поиска")

        # Свайпом убираем то, что выше строки поиска: верх ленты встаёт под неё.
        swipe_length = search.bounds.top - feed.bounds.top
        await self.device.swipe(
            feed.bounds.center.x,
            feed.bounds.center.y,
            feed.bounds.center.x,
            feed.bounds.center.y - swipe_length,
            duration=swipe_length,
        )

        # Ждём строку поиска (а не мгновенный find) — после свайпа дерево перестраивается.
        search = await self.device.wait_for(
            GoogleSelectors.SEARCH, timeout=self.config.ready_timeout
        )
        if search.bounds is None:
            raise RuntimeError("у строки поиска нет границ после свайпа")

        bounds = Bounds(
            feed.bounds.left,
            search.bounds.bottom,
            feed.bounds.right,
            feed.bounds.bottom,
        )
        logger.info("[%s] рабочая область определена: %s", self.device.serial, bounds)
        return bounds

    # --------------------------------------------------------------------- #
    # Свайпы
    # --------------------------------------------------------------------- #

    @property
    def _band(self) -> tuple[int, int, int]:
        """Полоса свайпа внутри рабочей области.

        Returns:
            Кортеж ``(x, y_top, y_bottom)``: X по центру ленты и крайние Y с
            отступом 12.5% от верха и низа рабочей области.
        """
        ws = self.working_bounds
        margin = int(ws.height * 0.125)
        return ws.center.x, ws.top + margin, ws.bottom - margin

    async def _swipe(self, y_from: int, y_to: int) -> None:
        """Выполнить вертикальный свайп по центру ленты.

        Args:
            y_from: Начальная Y-координата (где «прижимается» палец).
            y_to: Конечная Y-координата (куда ведём палец).
        """
        x, *_ = self._band
        await self.device.swipe(x, y_from, x, y_to, duration=self.working_bounds.height)

    async def swipe_forward(self) -> None:
        """Пролистнуть ленту на страницу вперёд (содержимое уходит вверх)."""
        _, y_top, y_bottom = self._band
        await self._swipe(y_bottom, y_top)

    # --------------------------------------------------------------------- #
    # Ожидание загрузки ленты
    # --------------------------------------------------------------------- #

    def _feed_signature(self, tree: UiTree) -> tuple[str, int] | None:
        """Вычислить подпись верха ленты для детекта остановки прокрутки/загрузки.

        Args:
            tree: UI-дамп, по которому считаем подпись.

        Returns:
            Кортеж ``(текст, верхняя_Y)`` первого видимого подписанного элемента
            ленты, либо ``None`` если ленты/подписей ещё нет.
        """
        feed = tree.find(GoogleSelectors.FEED)
        if feed is None:
            return None
        for node in feed.descendants():
            label = (node.text or node.content_desc or "").strip()
            if label and node.bounds is not None:
                return (label, node.bounds.top)
        return None

    async def wait_feed_settled(self) -> UiTree:
        """Дождаться, пока лента догрузится и перестанет меняться.

        Адаптивно к скорости интернета: сначала ждём появления ленты, затем
        опрашиваем дампы, пока подпись её верха не перестанет меняться
        :attr:`SETTLE_STABLE` раз подряд — либо пока не истечёт
        :attr:`SETTLE_TIMEOUT`.

        Returns:
            Последний снятый UI-дамп (уже «успокоившейся» ленты).

        Raises:
            axonctl.WaitTimeout: Если лента не появилась за :attr:`READY_TIMEOUT`.
        """
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.config.ready_timeout)

        prev: tuple[str, int] | None = None
        stable = 0
        tree = await self.device.dump()
        for _ in range(max(1, int(self.config.settle_timeout / self.config.settle))):
            tree = await self.device.dump()
            sig = self._feed_signature(tree)
            if sig is not None and sig == prev:
                stable += 1
                if stable >= self.config.settle_stable:
                    logger.debug("[%s] лента успокоилась", self.device.serial)
                    break
            else:
                stable = 0
            prev = sig
            await asyncio.sleep(self.config.settle)
        else:
            logger.debug(
                "[%s] лента не успокоилась за %.1fс", self.device.serial, self.config.settle_timeout
            )
        return tree

    # --------------------------------------------------------------------- #
    # Поиск и захват рекламы
    # --------------------------------------------------------------------- #

    async def get_sponsored_block(self) -> UiNode | None:
        """Найти в текущем дампе рекламную карточку, видимую в рабочей области.

        Карточка — это прямой ребёнок ленты, в чьих границах целиком лежит
        пометка «Sponsored».

        Returns:
            Узел рекламной карточки, либо ``None`` если рекламы в кадре нет.

        Raises:
            RuntimeError: Если в дампе нет ленты Discover.
        """
        sponsored = self._root.find(Selector.text("Sponsored"))
        feed = self._root.find(GoogleSelectors.FEED)

        if feed is None or feed.bounds is None:
            raise RuntimeError("не найдена лента Discover")
        if sponsored is None or sponsored.bounds is None:
            return None
        # Пометка вне рабочей области — карточку целиком не снять, пропускаем.
        if (
            sponsored.bounds.bottom < self.working_bounds.top
            or sponsored.bounds.top > self.working_bounds.bottom
        ):
            return None

        block = next(
            (
                child
                for child in feed.children
                if child.bounds
                and child.bounds.top < sponsored.bounds.top
                and child.bounds.bottom > sponsored.bounds.bottom
            ),
            None,
        )
        if block is not None:
            logger.info("[%s] найдена реклама в ленте", self.device.serial)
        return block

    async def tighten_sponsored(self, target_bottom: int) -> None:
        """Подвести ``target_bottom`` рекламы к низу рабочей области (содержимое вниз).

        Доскролливаем порциями по высоте полосы свайпа, пока заданная граница не
        окажется у низа рабочей области (в пределах :attr:`ALIGN_TOLERANCE`). Если
        у рекламы есть блок оценки, ``target_bottom`` — это его верх, чтобы оценка
        ушла ниже рабочей области и не попала в общий скрин.

        Args:
            target_bottom: Y-координата границы рекламы, которую ведём к низу области.
        """
        _, y_top, y_bottom = self._band
        span = y_bottom - y_top
        distance = self.working_bounds.bottom - target_bottom

        logger.debug("[%s] довожу рекламу вниз на %dpx", self.device.serial, distance)
        while distance >= self.config.align_tolerance:
            step = min(distance, span)
            await self._swipe(y_top, y_top + step)
            distance -= step

    async def take_screenshot(self) -> Image.Image:
        """Снять скриншот экрана и декодировать его в PIL-изображение.

        Returns:
            Изображение всего экрана устройства (PNG).
        """
        raw = await self.device.screenshot(format="png")
        return Image.open(BytesIO(raw))

    # --------------------------------------------------------------------- #
    # Разбор рекламной карточки
    # --------------------------------------------------------------------- #

    def _video_node(self, card: UiNode) -> UiNode | None:
        """Найти узел видео в карточке (видео-плеер или video_frame).

        Returns:
            Узел видео, пригодный для тапа, либо ``None`` если рекламы-видео нет.
        """
        for node in card.descendants():
            if node.center is None:
                continue
            rid = node.resource_id or ""
            cls = node.class_name or ""
            desc = node.content_desc or ""
            if (
                any(m in rid for m in self.VIDEO_ID_MARKERS)
                or any(v in cls for v in self.VIDEO_CLASSES)
                or desc.startswith("Video ")
            ):
                return node
        return None

    def _ad_text_node(self, card: UiNode) -> UiNode | None:
        """Найти текстовый узел рекламы для тапа — заголовок (самый длинный текст).

        Returns:
            Узел с самым длинным текстом (кроме «Sponsored»), пригодный для тапа,
            либо ``None`` если подходящего текста нет.
        """
        best: UiNode | None = None
        best_len = 0
        for node in card.descendants():
            text = (node.text or "").strip()
            if not text or text == "Sponsored" or node.center is None:
                continue
            if len(text) > best_len:
                best, best_len = node, len(text)
        return best

    def _footer_node(self, card: UiNode) -> UiNode | None:
        """Футер карточки — узел, среди прямых детей которого есть «Sponsored»."""
        for node in card.descendants():
            for child in node.children:
                if (child.text or "").strip() == "Sponsored":
                    return node
        return None

    def detect_form(self, card: UiNode) -> str:
        """Определить форму рекламы: ``video`` / ``carousel`` / ``image``."""
        for node in card.descendants():
            rid = node.resource_id or ""
            cls = node.class_name or ""
            desc = node.content_desc or ""
            if (
                any(m in rid for m in self.VIDEO_ID_MARKERS)
                or any(v in cls for v in self.VIDEO_CLASSES)
                or desc.startswith("Video ")
            ):
                return "video"
        for node in card.descendants():
            if any(c in (node.class_name or "") for c in self.CAROUSEL_CLASSES):
                return "carousel"
        return "image"

    def _is_google_play(self, card: UiNode) -> bool:
        """Ведёт ли реклама на Google Play (по тексту канала/CTA) — такие пропускаем."""
        for node in card.descendants():
            blob = f"{node.text or ''} {node.content_desc or ''}"
            if "Google Play" in blob or "Install app" in blob:
                return True
        return False

    def parse_ad(self, card: UiNode) -> AdInfo:
        """Разобрать карточку: форма, заголовок, канал, медиа-область, признак Google Play.

        * Канал — текстовый сосед «Sponsored» в футере.
        * Заголовок — самый длинный текст вне футера.
        * Медиа-область — от верха карточки до верха заголовка (или до футера).

        Args:
            card: Узел рекламной карточки.

        Returns:
            Разобранные данные рекламы (:class:`AdInfo`).
        """
        form = self.detect_form(card)
        is_gp = self._is_google_play(card)

        footer = self._footer_node(card)
        footer_ids: set[int] = set()
        if footer is not None:
            footer_ids = {id(footer)} | {id(n) for n in footer.descendants()}

        channel: str | None = None
        if footer is not None:
            for child in footer.children:
                text = (child.text or "").strip()
                if text and text != "Sponsored" and not (child.class_name or "").endswith("Button"):
                    channel = text
                    break

        headline: UiNode | None = None
        best_len = -1
        for node in card.descendants():
            if id(node) in footer_ids:
                continue
            text = (node.text or "").strip()
            if not text or text in ("Sponsored", channel):
                continue
            if len(text) > best_len:
                headline, best_len = node, len(text)
        text = headline.text.strip() if headline is not None and headline.text else None

        card_box, media_box, media_bottom = self._ad_boxes(card, headline, footer)
        top = card.bounds.top if card.bounds is not None else None

        return AdInfo(form, text, channel, is_gp, card_box, media_box, top, media_bottom)

    def _rating_top(self, card: UiNode) -> int | None:
        """Верх блока оценки рекламы («Is this a good recommendation…»), если он есть.

        Returns:
            Y-координата верха блока оценки, либо ``None`` если оценки нет.
        """
        for node in card.descendants():
            blob = f"{node.text or ''} {node.content_desc or ''}".lower()
            if "recommendation" in blob and node.bounds is not None:
                return node.bounds.top
        return None

    def _content_bottom(self, card: UiNode) -> int:
        """Низ рекламы без блока оценки: верх оценки, иначе низ карточки.

        Именно эту границу подводим к низу рабочей области, чтобы блок оценки ушёл
        ниже неё и не попал в общий скрин.
        """
        rating_top = self._rating_top(card)
        if rating_top is not None:
            return rating_top
        if card.bounds is not None:
            return card.bounds.bottom
        return self.working_bounds.bottom

    def _ad_boxes(
        self, card: UiNode, headline: UiNode | None, footer: UiNode | None
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None, int | None]:
        """Посчитать прямоугольники общего скрина и медиа (прижатые к рабочей области).

        Низ общего скрина — это низ контента без блока оценки (см. :meth:`_content_bottom`).

        Returns:
            Кортеж ``(card_box, media_box, media_bottom)``: боксы (или ``None``) и
            неприжатый низ медиа-области (цель выравнивания при отдельном скрине).
        """
        if card.bounds is None:
            return None, None, None
        ws = self.working_bounds
        top = max(card.bounds.top, ws.top)
        bottom = min(self._content_bottom(card), ws.bottom)
        card_box = (card.bounds.left, top, card.bounds.right, bottom)

        # Низ медиа без прижатия к области — это цель выравнивания при отдельном
        # скрине картинки (подтянуть низ картинки к низу рабочей области).
        if headline is not None and headline.bounds is not None:
            media_bottom = headline.bounds.top
        elif footer is not None and footer.bounds is not None:
            media_bottom = footer.bounds.top
        else:
            media_bottom = None

        media_box = None
        if media_bottom is not None:
            media_bottom_box = min(media_bottom, ws.bottom)
            if media_bottom_box - top >= self.config.min_media_height:
                media_box = (card.bounds.left, top, card.bounds.right, media_bottom_box)
        return card_box, media_box, media_bottom

    # --------------------------------------------------------------------- #
    # Обработка рекламы: скрины, текст/канал, открытие и ссылки
    # --------------------------------------------------------------------- #

    async def process_ad(self, card: UiNode) -> None:
        """Обработать найденную рекламу: скрины, текст/канал, открытие и ссылки.

        Шаги: общий скрин → скрин медиа → текст и канал → открытие рекламы и сбор
        ссылок (для видео — сайт + видео, для картинки/карусели — только сайт;
        Google Play не открываем). Дубли (тот же канал+заголовок) пропускаются.

        Args:
            card: Узел рекламной карточки (с актуальными границами).
        """
        if card.bounds is None:
            return
        info = self.parse_ad(card)

        key = (info.channel, info.text)
        if key in self._seen:
            logger.info(
                "[%s] реклама уже собрана (%s) — пропускаю", self.device.serial, info.channel
            )
            return
        self._seen.add(key)

        # Свой каталог на рекламу; номер продолжает уже сохранённые на диске.
        self._ad_count += 1
        ad_dir = self._out_dir / f"ad{self._ad_count:03d}"
        ad_dir.mkdir(parents=True, exist_ok=True)

        # 1-2. Общий скрин карточки и скрин медиа (картинка/видео/карусель).
        # Если картинка целиком в области (верх карточки ниже верха области) — кропим
        # медиа из общего скрина. Иначе картинка вылезает выше — снимем её отдельно
        # после сбора ссылок (подтянув низ картинки к низу области).
        image = await self.take_screenshot()
        if info.card_box is not None:
            image.crop(info.card_box).save(ad_dir / "general.png")
        media_in_frame = info.top is not None and info.top > self.working_bounds.top
        if info.media_box is not None and media_in_frame:
            image.crop(info.media_box).save(ad_dir / "media.png")

        # 3-4. Текст и канал (+ структура и HTML для отладки) — пишем в meta.json ниже.
        save_node(card, ad_dir / "structure.json")
        await self.device.inspect(ad_dir / "ui.html")
        logger.info(
            "[%s] реклама [%s] канал=%r -> %s", self.device.serial, info.form, info.channel, ad_dir
        )

        # 5. Открытие рекламы и сбор ссылок (Google Play — пропускаем открытие).
        page_url: str | None = None  # реальный (полный) URL лендинга
        landing_url: str | None = None  # google-style share-ссылка
        video_url: str | None = None
        if info.is_google_play:
            logger.info("[%s] реклама ведёт на Google Play — не открываю", self.device.serial)
        elif await self._open_ad(info, card):
            try:
                await self._wait_ad_loaded()
                page_url = await self._grab_page_url()  # из page info
                landing_url = await self._grab_landing_url()  # google-style
                if info.form == "video":
                    video_url = await self._grab_video_url()
            except AxonError as exc:
                logger.warning("[%s] сбой при сборе ссылок: %s", self.device.serial, exc)
            finally:
                await self._back_to_feed()

        # Картинка вылезала выше области — снимаем её отдельно, подтянув низ к низу
        # области (делаем уже после возврата в ленту, чтобы не сбить открытие).
        if info.media_box is not None and not media_in_frame:
            await self._capture_media_aligned(ad_dir)

        (ad_dir / "meta.json").write_text(
            json.dumps(
                {
                    "form": info.form,
                    "text": info.text,
                    "channel": info.channel,
                    "google_play": info.is_google_play,
                    "page_url": page_url,
                    "landing_url": landing_url,
                    "video_url": video_url,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def _capture_media_aligned(self, ad_dir: Path) -> None:
        """Снять картинку рекламы, подтянув её низ к низу рабочей области.

        Нужно, когда верх карточки выше рабочей области (картинка не влезает целиком).
        Заново находим карточку в ленте, доводим низ медиа к низу области и кропим.
        Best-effort: при сбое просто не перезаписываем media.png.

        Args:
            ad_dir: Каталог рекламы, куда сохраняется ``media.png``.
        """
        try:
            self._root = await self.wait_feed_settled()
            card = await self.get_sponsored_block()
            if card is None:
                return
            info = self.parse_ad(card)
            if info.media_bottom is None:
                return

            await self.tighten_sponsored(info.media_bottom)  # низ картинки → низ области
            self._root = await self.wait_feed_settled()
            card = await self.get_sponsored_block()
            if card is None:
                return
            info = self.parse_ad(card)
            if info.media_box is None:
                return

            image = await self.take_screenshot()
            image.crop(info.media_box).save(ad_dir / "media.png")
            logger.info("[%s] картинка снята с выравниванием низа: %s", self.device.serial, ad_dir)
        except AxonError as exc:
            logger.warning("[%s] не удалось снять картинку отдельно: %s", self.device.serial, exc)

    async def _open_ad(self, info: AdInfo, card: UiNode) -> bool:
        """Тапнуть, чтобы открыть рекламу: видео — по видео, иначе — по тексту.

        Args:
            info: Разобранные данные рекламы (нужна форма).
            card: Узел рекламной карточки.

        Returns:
            ``True`` если тап выполнен, ``False`` если не нашли по чему тапнуть.
        """
        target = self._video_node(card) if info.form == "video" else self._ad_text_node(card)
        if target is None or target.center is None:
            logger.warning("[%s] не нашёл, по чему тапнуть [%s]", self.device.serial, info.form)
            return False
        # Тап по цели, но не выше рабочей области (если карточка выходит за кадр).
        ws = self.working_bounds
        y = max(ws.top, min(target.center.y, ws.bottom))
        await self.device.tap(target.center.x, y)
        logger.info("[%s] открываю рекламу [%s]", self.device.serial, info.form)
        return True

    async def _wait_ad_loaded(self) -> None:
        """Дождаться Chrome и загрузки сайта рекламы (с ограничением), затем «посмотреть»."""
        await self.device.wait_for(
            GoogleSelectors.CHROME_TOOL_BAR, timeout=self.config.ready_timeout
        )
        try:
            await self.device.wait_gone(
                GoogleSelectors.CHROME_PROGRESS_BAR, timeout=self.config.ad_load_timeout
            )
        except WaitTimeout:
            logger.info(
                "[%s] сайт рекламы грузится дольше %.0fс",
                self.device.serial,
                self.config.ad_load_timeout,
            )
        dwell = random.uniform(self.config.ad_dwell_min, self.config.ad_dwell_max)
        logger.info("[%s] смотрю сайт рекламы %.1fс", self.device.serial, dwell)
        await asyncio.sleep(dwell)

    @staticmethod
    def _extract_url(text: str) -> str | None:
        """Вытащить первую http(s)-ссылку из текста."""
        match = re.search(r"https?://\S+", text)
        return match.group(0) if match else None

    async def _grab_page_url(self) -> str | None:
        """Снять полный URL лендинга через попап «информация о странице».

        Тапаем адресную строку (``url_bar``) — открывается попап page info с усечённым
        до домена адресом; тапаем по нему (``page_info_truncated_url``), чтобы раскрыть
        полный адрес, и читаем его из ``page_info_url``. Попап закрываем в любом случае,
        иначе он перекроет тулбар и сломает следующий шаг. Best-effort: при сбое —
        ``None``.
        """
        try:
            # Короткий таймаут: page-info — необязательный шаг, флак не должен съедать
            # 15с и ломать сбор основной ссылки.
            bar = await self.device.wait_for(
                GoogleSelectors.CHROME_URL_BAR, timeout=self.config.ad_load_timeout
            )
            if bar.center is not None:
                await self.device.tap(bar.center.x, bar.center.y)
            try:
                # Тап по усечённому домену раскрывает полный URL.
                truncated = await self.device.wait_for(
                    GoogleSelectors.PAGE_INFO_TRUNCATED_URL, timeout=self.config.ad_load_timeout
                )
                if truncated.center is not None:
                    await self.device.tap(truncated.center.x, truncated.center.y)
                    await asyncio.sleep(self.config.settle)

                url_node = await self.device.wait_for(
                    GoogleSelectors.PAGE_INFO_URL, timeout=self.config.ad_load_timeout
                )
                url = (
                    self._extract_url(url_node.text or "") or (url_node.text or "").strip() or None
                )
                logger.info("[%s] полный URL лендинга: %s", self.device.serial, url)
                return url
            finally:
                # Попап перекрывает тулбар — закрываем его (back до Chrome).
                await self._back_until(GoogleSelectors.CHROME_TOOL_BAR)
        except AxonError as exc:
            logger.warning("[%s] не удалось получить URL лендинга: %s", self.device.serial, exc)
            return None

    async def _grab_landing_url(self) -> str | None:
        """Снять ссылку на сайт рекламы через кнопку «Share link» в тулбаре Chrome.

        Дожидаемся тулбара, тапаем именно кнопку «Share link» (а не центр тулбара,
        где сама ссылка), читаем превью-текст со ссылкой и возвращаемся к Chrome.
        Best-effort: при сбое возвращаем ``None``.
        """
        try:
            await self.device.wait_for(
                GoogleSelectors.CHROME_TOOL_BAR, timeout=self.config.ready_timeout
            )

            # Кнопка «Share link» в тулбаре — тапаем её, а не центр тулбара (ссылку).
            share = await self.device.wait_for(
                GoogleSelectors.SHARE_LINK, timeout=self.config.ready_timeout
            )
            if share.center is not None:
                await self.device.tap(share.center.x, share.center.y)

            preview = await self.device.wait_for(
                GoogleSelectors.SHARE_PREVIEW_TEXT, timeout=self.config.ready_timeout
            )
            url = self._extract_url(preview.text or "")
            logger.info("[%s] ссылка на сайт: %s", self.device.serial, url)

            # Закрываем диалог шеринга — back, пока снова не окажемся в Chrome.
            await self._back_until(GoogleSelectors.CHROME_TOOL_BAR)
            return url
        except AxonError as exc:
            logger.warning("[%s] не удалось получить ссылку на сайт: %s", self.device.serial, exc)
            return None

    @staticmethod
    def _seekbar(player: UiNode) -> UiNode | None:
        """Найти ``SeekBar`` в поддереве плеера (с границами)."""
        return next(
            (
                n
                for n in player.descendants()
                if (n.class_name or "").endswith("SeekBar") and n.bounds
            ),
            None,
        )

    async def _grab_video_url(self) -> str | None:
        """Снять ссылку на видео через кнопку под полосой прокрутки плеера.

        Закрываем сайт (close_button) → плеер появляется с задержкой и в отдельном
        окне WebView, ищем его через ``device.find``. В контролах под ``SeekBar`` слева
        находится кнопка действия — берём её по геометрии (самый левый кликабельный
        узел ниже полосы), без привязки к подписи. После тапа: если открылся диалог с
        полем ``unified-share-url-input:0`` — читаем ссылку из него; если это была
        «Копировать ссылку» — читаем буфер обмена. Всё best-effort.
        """
        try:
            close = await self.device.wait_for(
                GoogleSelectors.CHROME_CLOSE, timeout=self.config.ready_timeout
            )
            if close.center is not None:
                await self.device.tap(close.center.x, close.center.y)
            await asyncio.sleep(self.config.settle * 2)  # плееру нужно время появиться

            # Плеер — в отдельном окне WebView: ищем через device.find, а не в дампе.
            player = await self.device.find(GoogleSelectors.PLAYER_CONTAINER)
            if player is None or player.center is None:
                logger.warning("[%s] плеер не найден — нет ссылки на видео", self.device.serial)
                return None

            # Полоса прокрутки видна только при показанных контролах. Нет — тап по
            # плееру их показывает; затем поллим SeekBar (контролы рисуются не сразу).
            # Повторно НЕ тапаем — это toggle, второй тап их бы скрыл.
            seek = self._seekbar(player)
            if seek is None:
                await self.device.tap(player.center.x, player.center.y)
                for _ in range(max(1, int(self.config.ad_load_timeout / self.config.settle))):
                    await asyncio.sleep(self.config.settle)
                    player = await self.device.find(GoogleSelectors.PLAYER_CONTAINER)
                    if player is None:
                        break
                    seek = self._seekbar(player)
                    if seek is not None:
                        break
            if player is None or seek is None or seek.bounds is None:
                logger.warning("[%s] полоса прокрутки плеера не найдена", self.device.serial)
                return None

            # Кнопка действия — самый левый кликабельный узел под полосой прокрутки.
            button = min(
                (
                    n
                    for n in player.descendants()
                    if n.clickable and n.center is not None and n.center.y > seek.bounds.bottom
                ),
                key=lambda n: n.center.x,  # type: ignore[union-attr]
                default=None,
            )
            if button is None or button.center is None:
                logger.warning("[%s] кнопка под полосой прокрутки не найдена", self.device.serial)
                return None
            await self.device.tap(button.center.x, button.center.y)

            # Если это «Поделиться» — появится диалог с полем ссылки (даём ему время).
            # Если поле не появилось — это была «Копировать ссылку», читаем буфер.
            try:
                field = await self.device.wait_for(
                    GoogleSelectors.VIDEO_SHARE_URL, timeout=self.config.ad_load_timeout
                )
            except WaitTimeout:
                field = None
            if field is not None:
                url = self._extract_url(field.text or "") or (field.text or "").strip() or None
                logger.info("[%s] ссылка на видео (поделиться): %s", self.device.serial, url)
                return url
            url = await self._read_clipboard()
            logger.info("[%s] ссылка на видео (буфер): %s", self.device.serial, url)
            return url
        except AxonError as exc:
            logger.warning("[%s] не удалось получить ссылку на видео: %s", self.device.serial, exc)
            return None

    async def _read_clipboard(self) -> str | None:
        """Прочитать буфер обмена через вставку в поле поиска.

        Прямого чтения буфера на устройстве нет (cmd clipboard не реализован,
        Android блокирует фоновое чтение), поэтому возвращаемся в ленту, фокусируем
        строку поиска, вставляем буфер (KEYCODE_PASTE) и читаем текст поля. За собой
        очищаем поле и закрываем поиск. Best-effort.
        """
        if not await self._back_until(GoogleSelectors.FEED):
            return None
        box = await self.device.find(GoogleSelectors.SEARCH)
        if box is None or box.center is None:
            return None

        await self.device.tap(box.center.x, box.center.y)
        await asyncio.sleep(self.config.settle)
        if await self.device.find(GoogleSelectors.EDIT_TEXT) is None:
            return None

        await self.device.set_text(GoogleSelectors.EDIT_TEXT, "")
        adb = self.device._require_adb()
        await adb.shell(self.device.serial, "input keyevent 279")  # KEYCODE_PASTE
        await asyncio.sleep(self.config.settle)

        field = await self.device.find(GoogleSelectors.EDIT_TEXT)
        text = (field.text or "") if field is not None else ""

        await self.device.set_text(GoogleSelectors.EDIT_TEXT, "")  # убираем за собой
        await self.device.global_action("back")  # закрыть клавиатуру/поиск
        return self._extract_url(text)

    async def _back_until(self, selector: Selector, attempts: int | None = None) -> bool:
        """Жать back, пока на экране не появится ``selector`` (или не кончатся попытки)."""
        attempts = attempts or self.config.back_attempts
        for _ in range(attempts):
            if await self.device.find(selector) is not None:
                return True
            await self.device.global_action("back")
            await asyncio.sleep(self.config.settle)
        return await self.device.find(selector) is not None

    async def _back_to_feed(self) -> None:
        """Вернуться в ленту: жать back, пока не появится FEED."""
        if await self._back_until(GoogleSelectors.FEED):
            logger.info("[%s] вернулись в ленту, реклама осталась", self.device.serial)
        else:
            logger.warning(
                "[%s] не удалось вернуться в ленту за %d back",
                self.device.serial,
                self.config.back_attempts,
            )

    # --------------------------------------------------------------------- #
    # Обновление ленты
    # --------------------------------------------------------------------- #

    async def _wait_refresh_done(self, region_bottom: int) -> None:
        """Дождаться появления и исчезновения индикатора обновления ленты.

        Pull-to-refresh показывает элемент с ``content-desc`` «Google» в полосе над
        строкой поиска (y от 0 до ``region_bottom``): его появление означает, что
        обновление пошло, исчезновение — что завершилось. Ищем строго в этой полосе,
        чтобы не спутать с другими элементами «Google» на экране. Best-effort.

        Args:
            region_bottom: Нижняя граница полосы поиска индикатора (``search.bounds.top``).
        """

        async def present() -> bool:
            tree = await self.device.dump()
            return any(
                node.bounds is not None
                and node.bounds.top >= 0
                and node.bounds.bottom <= region_bottom
                for node in tree.find_all(Selector.desc("Google"))
            )

        poll = 0.2
        # Появление индикатора (обновление началось).
        for _ in range(int(self.config.ready_timeout / poll)):
            if await present():
                break
            await asyncio.sleep(poll)
        # Исчезновение индикатора (обновление завершилось).
        for _ in range(int(self.config.ready_timeout / poll)):
            if not await present():
                logger.info("[%s] обновление ленты завершилось", self.device.serial)
                return
            await asyncio.sleep(poll)
        logger.warning(
            "[%s] индикатор обновления не исчез за %.0fс",
            self.device.serial,
            self.config.ready_timeout,
        )

    async def update_feed(self) -> None:
        """Обновить ленту перед закрытием: прокрутить наверх и сделать pull-to-refresh.

        Тап по вкладке «Home» возвращает ленту к началу, повторный тап (когда уже
        наверху) запускает обновление; затем добиваем быстрым свайпом сверху вниз.
        Делается best-effort: ошибки логируются, но не пробрасываются — обновление
        не критично, ведь приложение всё равно переоткрывается со свежей лентой.
        """
        _, y_top, y_bottom = self._band
        cx = self.working_bounds.center.x
        try:
            # Короткий свайп вверх по ленте выдвигает нижнюю панель навигации с
            # кнопкой «Home» — при листании вперёд она прячется, и без этого шага
            # тап по «Home» не находит цель (NodeNotFound).
            reveal = self.screen_height - self.working_bounds.bottom
            await self.device.swipe(
                cx, y_top, cx, y_top + reveal, duration=self.working_bounds.height
            )

            # Вкладка «Home» не поддерживает a11y-action `click` (ACTION_NOT_SUPPORTED),
            # поэтому тапаем по её координатам. Первый тап — лента наверх, повторный
            # (когда уже наверху) — обновление.
            home = await self.device.wait_for(
                Selector.text("Home"), timeout=self.config.ready_timeout
            )
            if home.center is None:
                logger.warning("[%s] у вкладки Home нет координат", self.device.serial)
                return
            await asyncio.sleep(0.5)

            await self.device.tap(home.center.x, home.center.y)
            await self.device.tap(home.center.x, home.center.y)
            # Дождаться, что лента перерисовалась (вернулась строка поиска).
            search = await self.device.wait_for(
                GoogleSelectors.SEARCH, timeout=self.config.ready_timeout
            )
            await self.wait_feed_settled()

            # Pull-to-refresh: быстрый свайп сверху вниз в рабочей области.
            await self.device.swipe(cx, y_top, cx, y_bottom, duration=100)

            # Дождаться, пока индикатор обновления (desc «Google» над строкой поиска)
            # появится и исчезнет — значит лента реально обновилась.
            if search.bounds is not None:
                await self._wait_refresh_done(search.bounds.top)

            await asyncio.sleep(self.config.settle)
            logger.info("[%s] лента обновлена перед закрытием", self.device.serial)
        except AxonError as exc:
            logger.warning("[%s] не удалось обновить ленту: %s", self.device.serial, exc)

    # --------------------------------------------------------------------- #
    # Основной цикл
    # --------------------------------------------------------------------- #

    async def _scan_and_capture(self) -> None:
        """Найти рекламу в текущем дампе, при наличии — выровнять, снять и пропустить."""
        sponsored = await self.get_sponsored_block()
        if sponsored is None or sponsored.bounds is None:
            return

        # Выравниваем по низу контента (без блока оценки) — чтобы оценка ушла ниже
        # рабочей области и не попала в общий скрин.
        await self.tighten_sponsored(self._content_bottom(sponsored))

        # После доводки карточка сместилась и могла догружаться — ждём и перечитываем.
        self._root = await self.wait_feed_settled()
        sponsored = await self.get_sponsored_block()
        if sponsored is not None:
            await self.process_ad(sponsored)

        # Несколько свайпов, чтобы уехать от рекламы (в т.ч. пропущенной Google Play)
        # и не найти её снова.
        for _ in range(self.config.post_ad_swipes):
            await self.swipe_forward()

    async def parse_feed(self, swipes: int | None = None) -> None:
        """Листать ленту, собирая рекламу.

        Каждая итерация изолирована: транзиентный сбой (таймаут загрузки, обрыв
        связи, устаревшее дерево) логируется и пропускается, не роняя всю сессию.

        Args:
            swipes: Сколько свайпов (итераций) сделать за сессию; ``None`` — взять из
                конфига (:attr:`~googleadsparser.config.ScrapeConfig.swipes`).
        """
        swipes = swipes if swipes is not None else self.config.swipes
        logger.info("[%s] листаю ленту: %d свайпов", self.device.serial, swipes)
        for i in range(swipes):
            try:
                self._root = await self.wait_feed_settled()
                await self._scan_and_capture()
                await self.swipe_forward()
            except AxonError as exc:
                # Случайно открылся Chrome — лента недоступна; прерываем сессию,
                # чтобы цикл закрыл Chrome (в kill) и перезапустил Google со свежей лентой.
                if await self.is_chrome_open():
                    logger.warning(
                        "[%s] открылся Chrome — прерываю сессию для перезапуска",
                        self.device.serial,
                    )
                    break
                logger.warning(
                    "[%s] свайп %d/%d пропущен из-за сбоя: %s",
                    self.device.serial,
                    i + 1,
                    swipes,
                    exc,
                )
                await asyncio.sleep(self.config.settle)
        logger.info("[%s] листание завершено", self.device.serial)

    async def _setup(self) -> None:
        """Запустить приложение и определить рабочую область, с повторами при сбоях.

        Raises:
            RuntimeError: Если приложение не удалось запустить за
                :attr:`LAUNCH_ATTEMPTS` попыток.
        """
        for attempt in range(1, self.config.launch_attempts + 1):
            try:
                await self.launch()
                await self.capture_screen_size()
                self.working_bounds = await self.get_working_bounds()
                logger.info("[%s] готов к работе", self.device.serial)
                return
            except AxonError as exc:
                logger.warning(
                    "[%s] запуск, попытка %d/%d не удалась: %s",
                    self.device.serial,
                    attempt,
                    self.config.launch_attempts,
                    exc,
                )
                await self.kill()  # сбрасываем состояние перед повтором
                await asyncio.sleep(self.config.settle * attempt)  # линейный backoff
        raise RuntimeError("не удалось запустить приложение после нескольких попыток")

    async def run(self) -> None:
        """Бесконечный прогон на устройстве.

        Каждый цикл: запуск → листание ленты → обновление ленты → полное закрытие.
        Следующий цикл открывает приложение заново — это и есть «обновление» ленты
        свежими объявлениями. Сбой цикла (не поднялось приложение и т.п.) логируется,
        и после паузы цикл повторяется — прогон не останавливается.
        """
        logger.info("[%s] старт прогона", self.device.serial)
        cycle = 0
        try:
            while True:
                cycle += 1
                logger.info("[%s] цикл %d", self.device.serial, cycle)
                try:
                    await self._setup()
                    await self.parse_feed()
                    await self.update_feed()  # освежить ленту перед перезапуском
                except (AxonError, RuntimeError) as exc:
                    logger.error("[%s] цикл %d прерван: %s", self.device.serial, cycle, exc)
                finally:
                    # Полностью закрываем приложение — следующий цикл откроет его заново.
                    await self.kill()
                await asyncio.sleep(self.config.settle)
        finally:
            await self.kill()
            logger.info("[%s] прогон остановлен", self.device.serial)


async def google_parser(device: Device, config: ScrapeConfig | None = None) -> None:
    """Сценарий для :meth:`~axonctl.FleetController.run`: свежий парсер на устройство.

    Args:
        device: Устройство, переданное контроллером флота.
        config: Параметры скрейпинга; ``None`` — значения по умолчанию.
    """
    await GoogleParser(device, config).run()
