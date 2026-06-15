"""Парсер рекламы из ленты Google Discover (приложение Google для Android).

Сценарий на каждое устройство (:class:`GoogleParser`):

1. Запустить приложение Google и дождаться его на переднем плане.
2. Определить рабочую область ленты (между строкой поиска и низом ленты).
3. Листать ленту медленными свайпами, выискивая карточки с пометкой «Sponsored».
4. Найдя рекламу — подвести её низ к низу рабочей области, снять скрин и сохранить
   (скрин + структуру) в отдельный каталог ``adNNN`` со сквозной нумерацией, затем
   кликнуть по ней (открыть Chrome и вернуться back), чтобы она не пропала из ленты.
5. Терпеть транзиентные сбои (медленный интернет, обрыв связи, устаревшее дерево):
   ожидания адаптивны, ошибки изолированы по итерациям, запуск — с повторами.

Запуск выполняется через :class:`~axonctl.FleetController` над всеми устройствами,
описанными в ``fleet.toml``.
"""

import asyncio
import json
import logging
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
    CHROME_TOOL_BAR = Selector.id("com.android.chrome:id/toolbar")
    CHROME_PROGRESS_BAR = Selector.id("com.android.chrome:id/toolbar_progress_bar")


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

    async def parse_sponsored(self, sponsored: UiNode) -> None:
        """Снять скрин рекламы, обрезать по её границам и сохранить.

        Каждая реклама кладётся в собственный каталог ``adNNN`` (нумерация сквозная
        и продолжается после перезапуска скрипта — см. :meth:`_last_ad_index`):

        * ``screenshot.png`` — обрезанный по карточке скрин;
        * ``structure.json`` — UI-структура карточки.

        Кроп прижимается к рабочей области по вертикали, чтобы в кадр не попали
        строка поиска сверху и навигация снизу.

        Args:
            sponsored: Узел рекламной карточки (с актуальными границами).
        """
        if sponsored.bounds is None:
            return

        ws = self.working_bounds
        box = (
            sponsored.bounds.left,
            max(sponsored.bounds.top, ws.top),  # не залезаем выше рабочей области
            sponsored.bounds.right,
            min(sponsored.bounds.bottom, ws.bottom),
        )

        # Свой каталог на рекламу; номер продолжает уже сохранённые на диске.
        self._ad_count += 1
        ad_dir = self._out_dir / f"ad{self._ad_count:03d}"
        ad_dir.mkdir(parents=True, exist_ok=True)

        image = await self.take_screenshot()
        image.crop(box).save(ad_dir / "screenshot.png")
        save_node(sponsored, ad_dir / "structure.json")
        await self.device.inspect(ad_dir / "ui.html")  # интерактивный HTML-снимок экрана
        logger.info("[%s] реклама сохранена в %s", self.device.serial, ad_dir)

        # Кликаем по рекламе (откроется Chrome и вернёмся), чтобы она не пропала из ленты.
        await self.click_ad(sponsored)

    def _ad_text_node(self, card: UiNode) -> UiNode | None:
        """Найти текстовый узел рекламы для тапа — заголовок (самый длинный текст).

        Args:
            card: Узел рекламной карточки.

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

    async def click_ad(self, sponsored: UiNode) -> None:
        """Тапнуть по тексту рекламы, дождаться загрузки Chrome и вернуться назад.

        Клик открывает Chrome с лендингом; ждём появления Chrome, затем окончания
        загрузки (исчезновения :attr:`GoogleSelectors.CHROME_PROGRESS_BAR`) и жмём
        back. Такое «взаимодействие» регистрируется, благодаря чему реклама не
        пропадает из ленты.

        Args:
            sponsored: Узел рекламной карточки (с актуальными границами).
        """
        target = self._ad_text_node(sponsored)
        if target is None or target.center is None:
            logger.warning("[%s] не нашёл текст рекламы для тапа", self.device.serial)
            return

        # Тап по тексту, но не выше рабочей области (если карточка выходит за кадр).
        ws = self.working_bounds
        y = max(ws.top, min(target.center.y, ws.bottom))
        await self.device.tap(target.center.x, y)

        # Дожидаемся открытия Chrome, затем окончания загрузки страницы. Если сайт
        # рекламы грузится дольше AD_LOAD_TIMEOUT — не ждём, закрываем и листаем дальше.
        await self.device.wait_for(GoogleSelectors.CHROME_TOOL_BAR, timeout=self.READY_TIMEOUT)
        try:
            await self.device.wait_gone(
                GoogleSelectors.CHROME_PROGRESS_BAR, timeout=self.AD_LOAD_TIMEOUT
            )
        except WaitTimeout:
            logger.info(
                "[%s] сайт рекламы грузится дольше %.0fс — закрываю",
                self.device.serial,
                self.AD_LOAD_TIMEOUT,
            )

        # Возвращаемся в ленту Google (закрываем сайт рекламы).
        await self.device.global_action("back")
        await self.device.wait_for(GoogleSelectors.FEED, timeout=self.READY_TIMEOUT)
        logger.info("[%s] реклама открыта и закрыта (back), осталась в ленте", self.device.serial)

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
            await self.parse_sponsored(sponsored)

        await self.swipe_forward()  # пропускаем рекламу, чтобы не найти её снова

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


asyncio.run(main())
