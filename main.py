"""Парсер рекламы из ленты Google Discover (приложение Google для Android).

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

Запуск выполняется через :class:`~axonctl.FleetController` над всеми устройствами,
описанными в ``fleet.toml``.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from axonctl import (
    AxonError,
    Bounds,
    Device,
    FleetController,
    Selector,
    UiNode,
    UiTree,
    WaitTimeout,
    retry_on_stale,
)
from PIL import Image

logger = logging.getLogger("googleadsparser")


class GoogleSelectors:
    """Селекторы ключевых элементов интерфейса приложения Google.

    Attributes:
        ROOT: Корневой контейнер приложения — его исчезновение означает, что
            приложение закрыто.
        FEED: Лента Discover (``RecyclerView``), которую листаем и в которой ищем рекламу.
        TOP_BAR: Верхняя панель — её низ задаёт верхнюю границу рабочей области.
        SEARCH: Поисковая строка — появляется раньше ленты и служит маркером готовности.
    """

    ROOT = Selector.id("com.google.android.googlequicksearchbox:id/googleapp_root")
    FEED = Selector.id(
        "com.google.android.googlequicksearchbox:id/googleapp_discover_recycler_view"
    )
    TOP_BAR = Selector.id("top_bar_compose_root")
    SEARCH = Selector.id("googleapp_facade_search_box")
    SPONSORED = Selector.text("Sponsored")
    CHROME_TOOL_BAR = Selector.id("com.android.chrome:id/toolbar")
    CHROME_PROGRESS_BAR = Selector.id("com.android.chrome:id/toolbar_progress_bar")
    #: Кнопка закрытия (крестик) в Chrome Custom Tab / плеере.
    CHROME_CLOSE = Selector.id("com.android.chrome:id/close_button")
    #: «Share link» в тулбаре Chrome — открывает диалог шеринга ссылки.
    SHARE_LINK = Selector.desc("Share link")
    #: Текст превью в системном диалоге шеринга — содержит ссылку на сайт рекламы.
    SHARE_PREVIEW_TEXT = Selector.id("android:id/content_preview_text")
    #: Адресная строка Chrome — тап открывает информацию о странице (page info).
    CHROME_URL_BAR = Selector.id("com.android.chrome:id/url_bar")
    #: Реальный домен лендинга в попапе «информация о странице» (усечён до домена).
    PAGE_INFO_URL = Selector.id("com.android.chrome:id/page_info_truncated_url")
    #: Контейнер видео-плеера (внутри WebView рекламы-видео).
    PLAYER_CONTAINER = Selector.id("playerContainer")
    #: Поле со ссылкой на видео в диалоге «Поделиться» плеера.
    VIDEO_SHARE_URL = Selector.id("unified-share-url-input:0")
    #: Любое редактируемое поле (для вставки буфера и чтения).
    EDIT_TEXT = Selector.cls("android.widget.EditText")


def save_node(node: UiNode, path: Path) -> None:
    """Сохранить UI-поддерево узла в JSON-файл.

    Временная отладочная утилита: помогает собирать реальные структуры рекламных
    карточек, чтобы уточнять селекторы и логику разбора.

    TODO: удалить, когда разбор карточек будет стабилизирован.

    Args:
        node: Узел, чьё поддерево сериализуется (вместе со всеми потомками).
        path: Путь к итоговому ``.json``-файлу; родительские каталоги создаются.
    """

    def to_dict(n: UiNode) -> dict:
        """Рекурсивно превратить узел и его потомков в JSON-совместимый словарь."""
        bounds = None
        if n.bounds is not None:
            bounds = {
                "left": n.bounds.left,
                "top": n.bounds.top,
                "right": n.bounds.right,
                "bottom": n.bounds.bottom,
            }
        center = None
        if n.center is not None:
            center = {"x": n.center.x, "y": n.center.y}
        return {
            "node_id": n.node_id,
            "class_name": n.class_name,
            "text": n.text,
            "resource_id": n.resource_id,
            "content_desc": n.content_desc,
            "clickable": n.clickable,
            "enabled": n.enabled,
            "focused": n.focused,
            "bounds": bounds,
            "center": center,
            "children": [to_dict(child) for child in n.children],
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(node), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("структура узла сохранена в %s", path)


@dataclass(frozen=True, slots=True)
class AdInfo:
    """Разобранные из карточки данные рекламы и геометрия для скринов.

    Attributes:
        form: Форма — ``"video"`` / ``"carousel"`` / ``"image"``.
        text: Заголовок рекламы (самый длинный текст карточки).
        channel: Название канала/рекламодателя.
        is_google_play: Ведёт ли реклама на Google Play (такие не открываем).
        card_box: Прямоугольник общего скрина карточки (прижат к рабочей области).
        media_box: Прямоугольник медиа (картинка/видео/карусель) или ``None``.
    """

    form: str
    text: str | None
    channel: str | None
    is_google_play: bool
    card_box: tuple[int, int, int, int] | None
    media_box: tuple[int, int, int, int] | None


class GoogleParser:
    """Скрейпер рекламы из ленты Google Discover на одном устройстве.

    Экземпляр привязан к конкретному устройству (создаётся по одному на устройство,
    чтобы не делить изменяемое состояние между параллельными прогонами).

    Attributes:
        device: Управляемое устройство.
        working_bounds: Рабочая область ленты; вычисляется в :meth:`get_working_bounds`.
    """

    APP: str = "com.google.android.googlequicksearchbox"
    #: Chrome — открывается при случайном тапе по рекламе; его надо закрывать.
    CHROME_APP: str = "com.android.chrome"
    #: Корневой каталог для собранной рекламы (по подкаталогу на устройство).
    OUTPUT_DIR: Path = Path("ads")
    #: Допуск выравнивания низа рекламы (px) — меньше уже не доскролливаем.
    ALIGN_TOLERANCE: int = 20
    #: Ожидание появления элементов (медленный старт / плохой интернет), с.
    READY_TIMEOUT: float = 15.0
    #: Сколько ждём загрузки сайта рекламы; дольше — закрываем и листаем дальше, с.
    AD_LOAD_TIMEOUT: float = 3.0
    #: Сколько «смотрим» сайт рекламы после загрузки перед закрытием (случайно), с.
    AD_DWELL_MIN: float = 2.0
    AD_DWELL_MAX: float = 5.0
    #: Сколько раз жмём back, возвращаясь из рекламы в ленту, прежде чем сдаться.
    BACK_ATTEMPTS: int = 5
    #: Сколько свайпов делать после рекламы (в т.ч. пропущенной), чтобы не найти её снова.
    POST_AD_SWIPES: int = 2
    #: Маркеры видео-рекламы в ``resource_id`` (видео — WebView внутри video_frame).
    VIDEO_ID_MARKERS: tuple[str, ...] = ("video_frame", "duration_badge", "webx_web_view")
    #: Классы видео-плеера в карточке.
    VIDEO_CLASSES: tuple[str, ...] = ("VideoView", "SurfaceView", "TextureView")
    #: Классы контейнера карусели (несколько креативов в одном объявлении).
    CAROUSEL_CLASSES: tuple[str, ...] = ("RecyclerView", "ViewPager")
    #: Минимальная высота медиа-области (px), иначе считаем, что её нет.
    MIN_MEDIA_HEIGHT: int = 80
    #: Базовая пауза «дать UI/ленте успокоиться», с.
    SETTLE: float = 0.5
    #: Сколько максимум ждать, пока лента догрузится и перестанет двигаться, с.
    SETTLE_TIMEOUT: float = 8.0
    #: Столько одинаковых дампов подряд считаем «лента успокоилась».
    SETTLE_STABLE: int = 2
    #: Попыток запустить приложение, прежде чем сдаться.
    LAUNCH_ATTEMPTS: int = 3

    def __init__(self, device: Device) -> None:
        """Инициализировать парсер для конкретного устройства.

        Args:
            device: Устройство, на котором будет работать парсер.
        """
        self.device = device
        #: Реальная ширина экрана в пикселях; заполняется в :meth:`capture_screen_size`.
        self.screen_width: int
        #: Реальная высота экрана в пикселях; заполняется в :meth:`capture_screen_size`.
        self.screen_height: int
        #: Рабочая область ленты; заполняется в :meth:`get_working_bounds`.
        self.working_bounds: Bounds
        #: Последний снятый UI-дамп; обновляется по ходу листания.
        self._root: UiTree
        #: Каталог собранной рекламы для этого устройства.
        self._out_dir = self.OUTPUT_DIR / device.serial
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
        await self.device.wait_activity(self.APP, timeout=self.READY_TIMEOUT)
        await self.device.wait_package(self.APP, timeout=self.READY_TIMEOUT)
        await self.device.wait_for(GoogleSelectors.SEARCH, timeout=self.READY_TIMEOUT)
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.READY_TIMEOUT)
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
            await self.device.wait_gone(GoogleSelectors.CHROME_TOOL_BAR, timeout=self.READY_TIMEOUT)
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
            await self.device.wait_gone(GoogleSelectors.ROOT, timeout=self.READY_TIMEOUT)
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
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.READY_TIMEOUT)

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
        search = await self.device.wait_for(GoogleSelectors.SEARCH, timeout=self.READY_TIMEOUT)
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
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.READY_TIMEOUT)

        prev: tuple[str, int] | None = None
        stable = 0
        tree = await self.device.dump()
        for _ in range(max(1, int(self.SETTLE_TIMEOUT / self.SETTLE))):
            tree = await self.device.dump()
            sig = self._feed_signature(tree)
            if sig is not None and sig == prev:
                stable += 1
                if stable >= self.SETTLE_STABLE:
                    logger.debug("[%s] лента успокоилась", self.device.serial)
                    break
            else:
                stable = 0
            prev = sig
            await asyncio.sleep(self.SETTLE)
        else:
            logger.debug(
                "[%s] лента не успокоилась за %.1fс", self.device.serial, self.SETTLE_TIMEOUT
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

    async def tighten_sponsored(self, sponsored_bounds: Bounds) -> None:
        """Подвести низ рекламы к низу рабочей области (содержимое уходит вниз).

        Доскролливаем порциями по высоте полосы свайпа, пока низ карточки не
        окажется у низа рабочей области (в пределах :attr:`ALIGN_TOLERANCE`).

        Args:
            sponsored_bounds: Текущие границы рекламной карточки.
        """
        _, y_top, y_bottom = self._band
        span = y_bottom - y_top
        distance = self.working_bounds.bottom - sponsored_bounds.bottom

        logger.debug("[%s] довожу рекламу вниз на %dpx", self.device.serial, distance)
        while distance >= self.ALIGN_TOLERANCE:
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

        card_box, media_box = self._ad_boxes(card, headline, footer)

        return AdInfo(form, text, channel, is_gp, card_box, media_box)

    def _ad_boxes(
        self, card: UiNode, headline: UiNode | None, footer: UiNode | None
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
        """Посчитать прямоугольники общего скрина и медиа (прижатые к рабочей области).

        Returns:
            Кортеж ``(card_box, media_box)``; любой элемент ``None``, если его нет.
        """
        if card.bounds is None:
            return None, None
        ws = self.working_bounds
        top = max(card.bounds.top, ws.top)
        bottom = min(card.bounds.bottom, ws.bottom)
        card_box = (card.bounds.left, top, card.bounds.right, bottom)

        if headline is not None and headline.bounds is not None:
            media_bottom = headline.bounds.top
        elif footer is not None and footer.bounds is not None:
            media_bottom = footer.bounds.top
        else:
            media_bottom = bottom
        media_bottom = min(media_bottom, ws.bottom)

        media_box = None
        if media_bottom - top >= self.MIN_MEDIA_HEIGHT:
            media_box = (card.bounds.left, top, card.bounds.right, media_bottom)
        return card_box, media_box

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
        image = await self.take_screenshot()
        if info.card_box is not None:
            image.crop(info.card_box).save(ad_dir / "general.png")
        if info.media_box is not None:
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

    async def _open_ad(self, info: AdInfo, card: UiNode) -> bool:
        """Тапнуть, чтобы открыть рекламу: видео — по видео, иначе — по тексту.

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
        await self.device.wait_for(GoogleSelectors.CHROME_TOOL_BAR, timeout=self.READY_TIMEOUT)
        try:
            await self.device.wait_gone(
                GoogleSelectors.CHROME_PROGRESS_BAR, timeout=self.AD_LOAD_TIMEOUT
            )
        except WaitTimeout:
            logger.info(
                "[%s] сайт рекламы грузится дольше %.0fс", self.device.serial, self.AD_LOAD_TIMEOUT
            )
        dwell = random.uniform(self.AD_DWELL_MIN, self.AD_DWELL_MAX)
        logger.info("[%s] смотрю сайт рекламы %.1fс", self.device.serial, dwell)
        await asyncio.sleep(dwell)

    @staticmethod
    def _extract_url(text: str) -> str | None:
        """Вытащить первую http(s)-ссылку из текста."""
        match = re.search(r"https?://\S+", text)
        return match.group(0) if match else None

    async def _grab_page_url(self) -> str | None:
        """Снять реальный домен лендинга через попап «информация о странице».

        Тапаем адресную строку (``url_bar``) — открывается попап с информацией о
        странице; читаем домен из ``page_info_truncated_url`` (полный URL там не
        показывается — он усечён до домена). Попап закрываем в любом случае, иначе он
        перекроет тулбар и сломает следующий шаг. Best-effort: при сбое — ``None``.
        """
        try:
            # Короткий таймаут: page-info — необязательный шаг, флак не должен съедать
            # 15с и ломать сбор основной ссылки.
            bar = await self.device.wait_for(
                GoogleSelectors.CHROME_URL_BAR, timeout=self.AD_LOAD_TIMEOUT
            )
            if bar.center is not None:
                await self.device.tap(bar.center.x, bar.center.y)
            try:
                url_node = await self.device.wait_for(
                    GoogleSelectors.PAGE_INFO_URL, timeout=self.AD_LOAD_TIMEOUT
                )
                url = (url_node.text or "").strip() or None
                logger.info("[%s] домен лендинга: %s", self.device.serial, url)
                return url
            finally:
                # Попап перекрывает тулбар — закрываем его (back до Chrome).
                await self._back_until(GoogleSelectors.CHROME_TOOL_BAR)
        except AxonError as exc:
            logger.warning("[%s] не удалось получить домен лендинга: %s", self.device.serial, exc)
            return None

    async def _grab_landing_url(self) -> str | None:
        """Снять ссылку на сайт рекламы через кнопку «Share link» в тулбаре Chrome.

        Дожидаемся тулбара, тапаем именно кнопку «Share link» (а не центр тулбара,
        где сама ссылка), читаем превью-текст со ссылкой и возвращаемся к Chrome.
        Best-effort: при сбое возвращаем ``None``.
        """
        try:
            await self.device.wait_for(GoogleSelectors.CHROME_TOOL_BAR, timeout=self.READY_TIMEOUT)

            # Кнопка «Share link» в тулбаре — тапаем её, а не центр тулбара (ссылку).
            share = await self.device.wait_for(
                GoogleSelectors.SHARE_LINK, timeout=self.READY_TIMEOUT
            )
            if share.center is not None:
                await self.device.tap(share.center.x, share.center.y)

            preview = await self.device.wait_for(
                GoogleSelectors.SHARE_PREVIEW_TEXT, timeout=self.READY_TIMEOUT
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
                GoogleSelectors.CHROME_CLOSE, timeout=self.READY_TIMEOUT
            )
            if close.center is not None:
                await self.device.tap(close.center.x, close.center.y)
            await asyncio.sleep(self.SETTLE * 2)  # плееру нужно время появиться

            # Плеер — в отдельном окне WebView: ищем через device.find, а не в дампе.
            player = await self.device.find(GoogleSelectors.PLAYER_CONTAINER)
            if player is None or player.center is None:
                logger.warning("[%s] плеер не найден — нет ссылки на видео", self.device.serial)
                return None

            # Полоса прокрутки видна только при показанных контролах. Нет — тап по
            # плееру их показывает (повторный тап их бы скрыл).
            seek = self._seekbar(player)
            if seek is None:
                await self.device.tap(player.center.x, player.center.y)
                await asyncio.sleep(self.SETTLE)
                player = await self.device.find(GoogleSelectors.PLAYER_CONTAINER)
                seek = self._seekbar(player) if player is not None else None
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
                    GoogleSelectors.VIDEO_SHARE_URL, timeout=self.AD_LOAD_TIMEOUT
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
        await asyncio.sleep(self.SETTLE)
        if await self.device.find(GoogleSelectors.EDIT_TEXT) is None:
            return None

        await self.device.set_text(GoogleSelectors.EDIT_TEXT, "")
        adb = self.device._require_adb()
        await adb.shell(self.device.serial, "input keyevent 279")  # KEYCODE_PASTE
        await asyncio.sleep(self.SETTLE)

        field = await self.device.find(GoogleSelectors.EDIT_TEXT)
        text = (field.text or "") if field is not None else ""

        await self.device.set_text(GoogleSelectors.EDIT_TEXT, "")  # убираем за собой
        await self.device.global_action("back")  # закрыть клавиатуру/поиск
        return self._extract_url(text)

    async def _back_until(self, selector: Selector, attempts: int | None = None) -> bool:
        """Жать back, пока на экране не появится ``selector`` (или не кончатся попытки)."""
        attempts = attempts or self.BACK_ATTEMPTS
        for _ in range(attempts):
            if await self.device.find(selector) is not None:
                return True
            await self.device.global_action("back")
            await asyncio.sleep(self.SETTLE)
        return await self.device.find(selector) is not None

    async def _back_to_feed(self) -> None:
        """Вернуться в ленту: жать back, пока не появится FEED."""
        if await self._back_until(GoogleSelectors.FEED):
            logger.info("[%s] вернулись в ленту, реклама осталась", self.device.serial)
        else:
            logger.warning(
                "[%s] не удалось вернуться в ленту за %d back",
                self.device.serial,
                self.BACK_ATTEMPTS,
            )

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
        for _ in range(int(self.READY_TIMEOUT / poll)):
            if await present():
                break
            await asyncio.sleep(poll)
        # Исчезновение индикатора (обновление завершилось).
        for _ in range(int(self.READY_TIMEOUT / poll)):
            if not await present():
                logger.info("[%s] обновление ленты завершилось", self.device.serial)
                return
            await asyncio.sleep(poll)
        logger.warning(
            "[%s] индикатор обновления не исчез за %.0fс", self.device.serial, self.READY_TIMEOUT
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
            home = await self.device.wait_for(Selector.text("Home"), timeout=self.READY_TIMEOUT)
            if home.center is None:
                logger.warning("[%s] у вкладки Home нет координат", self.device.serial)
                return
            await asyncio.sleep(0.5)

            await self.device.tap(home.center.x, home.center.y)
            await self.device.tap(home.center.x, home.center.y)
            # Дождаться, что лента перерисовалась (вернулась строка поиска).
            search = await self.device.wait_for(GoogleSelectors.SEARCH, timeout=self.READY_TIMEOUT)
            await self.wait_feed_settled()

            # Pull-to-refresh: быстрый свайп сверху вниз в рабочей области.
            await self.device.swipe(cx, y_top, cx, y_bottom, duration=100)

            # Дождаться, пока индикатор обновления (desc «Google» над строкой поиска)
            # появится и исчезнет — значит лента реально обновилась.
            if search.bounds is not None:
                await self._wait_refresh_done(search.bounds.top)

            await asyncio.sleep(self.SETTLE)
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

        await self.tighten_sponsored(sponsored.bounds)

        # После доводки карточка сместилась и могла догружаться — ждём и перечитываем.
        self._root = await self.wait_feed_settled()
        sponsored = await self.get_sponsored_block()
        if sponsored is not None:
            await self.process_ad(sponsored)

        # Несколько свайпов, чтобы уехать от рекламы (в т.ч. пропущенной Google Play)
        # и не найти её снова.
        for _ in range(self.POST_AD_SWIPES):
            await self.swipe_forward()

    async def parse_feed(self, swipes: int = 25) -> None:
        """Листать ленту, собирая рекламу.

        Каждая итерация изолирована: транзиентный сбой (таймаут загрузки, обрыв
        связи, устаревшее дерево) логируется и пропускается, не роняя всю сессию.

        Args:
            swipes: Сколько свайпов (итераций) сделать за сессию.
        """
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
                await asyncio.sleep(self.SETTLE)
        logger.info("[%s] листание завершено", self.device.serial)

    async def _setup(self) -> None:
        """Запустить приложение и определить рабочую область, с повторами при сбоях.

        Raises:
            RuntimeError: Если приложение не удалось запустить за
                :attr:`LAUNCH_ATTEMPTS` попыток.
        """
        for attempt in range(1, self.LAUNCH_ATTEMPTS + 1):
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
                    self.LAUNCH_ATTEMPTS,
                    exc,
                )
                await self.kill()  # сбрасываем состояние перед повтором
                await asyncio.sleep(self.SETTLE * attempt)  # линейный backoff
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
                await asyncio.sleep(self.SETTLE)
        finally:
            await self.kill()
            logger.info("[%s] прогон остановлен", self.device.serial)


async def dump(device: Device, path: str = "ui.html") -> None:
    """Сохранить интерактивный снимок текущего UI в HTML — утилита для отладки.

    Открыв полученный файл в браузере, можно осмотреть дерево элементов, их
    ``resource_id``/``text``/границы и подобрать селекторы. Удобно подключать
    временно: например, вызвать перед/после проблемного шага.

    Args:
        device: Устройство, с которого снимаем дамп.
        path: Куда сохранить HTML-инспектор.
    """
    await device.inspect(path)
    logger.info("[%s] UI-дамп сохранён в %s", device.serial, path)


async def google_parser(device: Device) -> None:
    """Сценарий для :meth:`FleetController.run`: свежий парсер на каждое устройство.

    Args:
        device: Устройство, переданное контроллером флота.
    """
    await GoogleParser(device).run()


async def main() -> None:
    """Точка входа: настроить логирование и запустить парсер на устройствах флота."""
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
