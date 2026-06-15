"""Парсинг рекламы из ленты Google Discover (com.google.android.googlequicksearchbox).

Сценарий:

1. Открыть приложение Google и дождаться его на переднем плане.
2. Определить рабочую область — от нижней границы ``top_bar_compose_root`` до
   нижней границы ленты ``...:id/googleapp_discover_recycler_view``.
3. Листать ленту медленными свайпами (без инерционного проскальзывания) в пределах
   рабочей области, отступая 0.25 её высоты от каждой границы.
4. Искать слово "Sponsored"; найдя — определить дочерний элемент ленты, который его
   содержит, подвести его нижнюю границу к низу рабочей области (чтобы реклама
   поместилась целиком) и сохранить скрин этого элемента.
5. Сделать два свайпа, чтобы не найти ту же рекламу повторно.
6. Отработав заданное число свайпов — полностью убить приложение, открыть заново и
   повторить.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from axonctl import (
    AxonError,
    Device,
    FleetController,
    Selector,
    UiNode,
    UiTree,
    WaitTimeout,
)
from PIL import Image

logger = logging.getLogger("googleadsparser")

#: Целевой пакет — приложение Google (поиск / Discover-лента).
GOOGLE_APP_PACKAGE = "com.google.android.googlequicksearchbox"

#: Лента Discover (скроллим её и ищем рекламу внутри).
RECYCLER_ID = "com.google.android.googlequicksearchbox:id/googleapp_discover_recycler_view"
#: Верхняя панель — её нижняя граница задаёт верх рабочей области.
TOP_BAR_ID = "top_bar_compose_root"
#: Маркер рекламы.
SPONSORED_TEXT = "Sponsored"
#: Если во время листания случайно открылся Chrome — детектируем по его тулбару.
CHROME_TOOLBAR_ID = "com.android.chrome:id/toolbar_container"
#: Подпись вкладки Home в нижней навигации (тап по ней прокручивает ленту наверх).
HOME_TAB_TEXT = "Home"


@dataclass(frozen=True, slots=True)
class ScrapeConfig:
    """Параметры одного прогона скрейпинга.

    Attributes:
        num_swipes: Сколько свайпов делать за одну сессию (между открытием и
            убийством приложения). Свайпы после рекламы тоже считаются.
        cycles: Сколько раз повторить цикл «открыть → листать → убить»;
            ``None`` — бесконечно.
        out_dir: Куда складывать скрины рекламы.
        edge_fraction: Доля высоты рабочей области, на которую отступаем от каждой
            её границы при свайпах (0.25 по условию).
        swipe_duration_ms: Минимальная длительность свайпа (мс). Свайп — ease-out с
            удержанием в конце, поэтому проскальзывания (fling) не возникает.
        max_swipe_velocity: Макс. средняя скорость свайпа (px/с). Длинные свайпы
            замедляем под неё, чтобы лента не уносилась по инерции при выравнивании.
        align_tolerance: Допуск (px) обрезки снизу — на сколько низ карточки может
            заходить за низ области, считаясь «выровненным».
        align_max_iters: Максимум итераций подгонки позиции карточки.
        min_drag: Минимальная длина свайпа (px). Короче — Android трактует жест как
            тап (открылась бы реклама). Любое доскролливание не короче этого.
        post_ad_swipes: Сколько свайпов делать после рекламы, чтобы не найти её снова.
        ready_timeout: Сколько ждать появления приложения на переднем плане (с).
        settle: Пауза после свайпа, чтобы дерево/рендер обновились (с).
        max_chrome_recoveries: Максимум подряд нажатий back, если случайно открылся
            Chrome; превышение прерывает сессию листания.
    """

    num_swipes: int = 30
    cycles: int | None = 3
    out_dir: Path = field(default_factory=lambda: Path("ads"))
    edge_fraction: float = 0.25
    swipe_duration_ms: int = 650
    max_swipe_velocity: float = 1100.0
    align_tolerance: int = 12
    align_max_iters: int = 6
    min_drag: int = 80
    post_ad_swipes: int = 3
    ready_timeout: float = 30.0
    settle: float = 0.5
    max_chrome_recoveries: int = 5


@dataclass(frozen=True, slots=True)
class Workspace:
    """Вертикальная рабочая область ленты и допустимая полоса для свайпов.

    Attributes:
        top: Верхняя граница области (нижняя граница ``top_bar_compose_root``).
        bottom: Нижняя граница области (нижняя граница ленты).
        left: Левая граница ленты (для X-координаты свайпа берём её центр).
        right: Правая граница ленты.
    """

    top: int
    bottom: int
    left: int
    right: int

    @property
    def height(self) -> int:
        """Высота рабочей области."""
        return self.bottom - self.top

    @property
    def center_x(self) -> int:
        """X-координата для вертикальных свайпов (центр ленты)."""
        return (self.left + self.right) // 2

    def band(self, edge_fraction: float) -> tuple[int, int]:
        """Полоса свайпа: отступ ``edge_fraction`` от верха и низа области.

        Returns:
            ``(band_top, band_bottom)`` — крайние Y, между которыми ведём палец.
        """
        margin = round(self.height * edge_fraction)
        return self.top + margin, self.bottom - margin


# --------------------------------------------------------------------------- #
# Подготовка устройства / открытие / закрытие приложения
# --------------------------------------------------------------------------- #

#: Команды adb shell для подготовки устройства (применяются один раз перед сбором).
#: Главное — зафиксировать портрет: в ландшафте ленты Discover нет и сбор падает.
_PREPARE_COMMANDS = (
    # 1. Выключить автоповорот и зафиксировать вертикальную (портретную) ориентацию.
    "settings put system accelerometer_rotation 0",
    "settings put system user_rotation 0",
    # 2. Выключить уведомления — режим «Не беспокоить» (чтобы ничего не всплывало).
    "cmd notification set_dnd on",
    # 3. Выключить системные жесты навигации (3-кнопочный режим, без edge-свайпов).
    "cmd overlay enable com.android.internal.systemui.navbar.threebutton",
    "settings put secure navigation_mode 0",
    # 4. Выключить все анимации.
    "settings put global window_animation_scale 0",
    "settings put global transition_animation_scale 0",
    "settings put global animator_duration_scale 0",
)


async def prepare_device(device: Device) -> None:
    """Подготовить устройство: портрет без автоповорота, без уведомлений/жестов/анимаций.

    Команды best-effort: на разных прошивках (например MIUI) часть настроек может не
    примениться — такие ошибки логируем и идём дальше.
    """
    adb = device._require_adb()  # публичного shell у Device нет — берём bound adb
    for cmd in _PREPARE_COMMANDS:
        try:
            await adb.shell(device.serial, cmd)
        except Exception as exc:
            logger.warning("[%s] не применилось %r: %s", device.serial, cmd, exc)
    logger.info(
        "[%s] устройство подготовлено (портрет, без уведомлений/жестов/анимаций)",
        device.serial,
    )


async def open_app(device: Device, *, package: str, ready_timeout: float) -> None:
    """Запустить приложение и дождаться его на переднем плане.

    Raises:
        AxonError: Если приложение не вышло на передний план вовремя.
        Exception: Если сорвался запуск через adb.
    """
    logger.info("[%s] запускаю %s", device.serial, package)
    await device.launch(package)
    await device.wait_package(package, timeout=ready_timeout)
    await device.sleep(1.0)  # дать ленте прогрузиться
    logger.info("[%s] %s на переднем плане", device.serial, package)


async def close_app(device: Device, *, package: str) -> None:
    """Принудительно закрыть приложение (force-stop). Ошибки логируем, не пробрасываем."""
    try:
        await device.kill(package)
        logger.info("[%s] %s закрыт", device.serial, package)
    except Exception as exc:
        logger.error("[%s] не удалось закрыть %s: %s", device.serial, package, exc)


# --------------------------------------------------------------------------- #
# Свайпы без проскальзывания (ease-out + удержание -> скорость отрыва = 0)
# --------------------------------------------------------------------------- #


def _ease_out_stroke(
    x: int, y1: int, y2: int, *, duration_ms: int, n_motion: int = 12, n_hold: int = 6
) -> dict:
    """Построить штрих жеста: ease-out от ``y1`` к ``y2`` плюс удержание в конце.

    Замедление к концу и несколько одинаковых финальных точек обнуляют скорость
    в момент отрыва пальца — Android не запускает инерционную прокрутку (fling).
    """
    points: list[dict[str, int]] = []
    for i in range(n_motion):
        t = i / (n_motion - 1)
        eased = 1.0 - (1.0 - t) ** 2  # быстро в начале, медленно в конце
        y = round(y1 + (y2 - y1) * eased)
        points.append({"x": x, "y": y})
    points.extend({"x": x, "y": y2} for _ in range(n_hold))
    return {"points": points, "duration": duration_ms, "startTime": 0}


async def slow_swipe(device: Device, x: int, y1: int, y2: int, *, duration_ms: int) -> None:
    """Медленный вертикальный свайп от ``(x, y1)`` к ``(x, y2)`` без проскальзывания.

    Note:
        Публичные ``device.swipe``/``device.drag`` — двухточечные штрихи с
        ненулевой скоростью отрыва и потому провоцируют fling. Многоточечный
        ease-out-штрих выражается только через сырой ``gesture``-RPC, поэтому
        здесь мы обращаемся к внутреннему слою ``device._conn.rpc`` (он помечен
        как implementation detail — единственное место, где мы его трогаем).
    """
    stroke = _ease_out_stroke(x, y1, y2, duration_ms=duration_ms)
    await device._conn.rpc.call("gesture", {"strokes": [stroke]})


async def scroll_by(device: Device, ws: Workspace, delta: int, cfg: ScrapeConfig) -> None:
    """Прокрутить содержимое ленты по вертикали на ``delta`` px (без проскальзывания).

    ``delta > 0`` — содержимое уходит вверх (листаем вперёд/вниз по ленте);
    ``delta < 0`` — содержимое идёт вниз. За один свайп двигаем не больше высоты
    разрешённой полосы; большие смещения дробим на несколько свайпов.

    Каждый отдельный свайп — не короче ``cfg.min_drag``: слишком короткий жест
    Android трактует как тап (открылась бы реклама/Chrome). Остаток короче
    минимума не доводим — это в пределах допустимой точности позиционирования.
    """
    band_top, band_bottom = ws.band(cfg.edge_fraction)
    band_height = band_bottom - band_top
    if band_height < cfg.min_drag:
        return
    remaining = delta
    while abs(remaining) >= cfg.min_drag:
        step = max(-band_height, min(band_height, remaining))
        if step > 0:  # содержимое вверх -> палец снизу вверх
            y1, y2 = band_bottom, band_bottom - step
        else:  # содержимое вниз -> палец сверху вниз
            y1, y2 = band_top, band_top - step
        # Длинные свайпы замедляем (ограничиваем среднюю скорость), чтобы лента не
        # уносилась по инерции и не «проскальзывала» при доводке до места.
        duration = max(cfg.swipe_duration_ms, round(abs(step) / cfg.max_swipe_velocity * 1000))
        await slow_swipe(device, ws.center_x, y1, y2, duration_ms=duration)
        await device.sleep(cfg.settle)
        remaining -= step


async def swipe_forward(device: Device, ws: Workspace, cfg: ScrapeConfig) -> None:
    """Один «страничный» свайп вперёд — на всю высоту разрешённой полосы."""
    band_top, band_bottom = ws.band(cfg.edge_fraction)
    await scroll_by(device, ws, band_bottom - band_top, cfg)


def _feed_top_signature(tree: UiTree) -> tuple[str, int] | None:
    """Подпись верха ленты (первый видимый элемент с подписью) — для детекта остановки прокрутки."""
    recycler = tree.find(Selector.id(RECYCLER_ID))
    if recycler is None:
        return None
    for node in recycler.descendants():
        label = (node.text or node.content_desc or "").strip()
        if label and node.bounds is not None:
            return (label, node.bounds.top)
    return None


async def _wait_feed_settled(
    device: Device, *, polls: int = 20, pause: float = 0.3, stable_rounds: int = 2
) -> None:
    """Дождаться, пока прокрутка ленты остановится (верх перестанет меняться)."""
    prev: tuple[str, int] | None = None
    stable = 0
    for _ in range(polls):
        sig = _feed_top_signature(await device.dump())
        if sig is not None and sig == prev:
            stable += 1
            if stable >= stable_rounds:  # подряд без изменений — прокрутка докрутилась
                return
        else:
            stable = 0
        prev = sig
        await device.sleep(pause)


async def refresh_feed(device: Device, ws: Workspace, cfg: ScrapeConfig) -> None:
    """Обновить ленту перед закрытием: нажать вкладку Home, дождаться верха, pull-to-refresh.

    1. Тап по вкладке «Home» в нижней навигации — лента прокручивается наверх.
    2. Ждём, пока лента докрутится до верха (прокрутка остановится).
    3. Быстрый свайп сверху вниз в рабочей области — pull-to-refresh.

    Всё best-effort: ошибки логируем и не пробрасываем.
    """
    try:
        # 1. Нажать Home. Вкладка активна и в a11y не «clickable», но raw-тап по
        #    её координате срабатывает. Ищем по подписи; иначе — левый нижний таб.
        tree = await device.dump()
        home = tree.find(Selector.text(HOME_TAB_TEXT))
        if home is not None and home.center is not None:
            await device.tap(home.center.x, home.center.y)
        else:
            # Запасной вариант: центр крайнего левого таба под лентой.
            await device.tap(ws.left + (ws.right - ws.left) // 8, ws.bottom + 84)
            logger.info("[%s] Home по координате (подпись не найдена)", device.serial)

        # 2. Дождаться, пока лента докрутится до верха.
        await _wait_feed_settled(device)

        # 3. Быстрый свайп сверху вниз в рабочей области — pull-to-refresh.
        cx = ws.center_x
        await device.swipe(cx, ws.top + 10, cx, ws.bottom - 10, duration=200)
        await device.sleep(1.5)  # дать ленте обновиться
        logger.info("[%s] лента обновлена перед закрытием", device.serial)
    except Exception as exc:  # refresh best-effort — не роняем цикл
        logger.warning("[%s] не удалось обновить ленту: %s", device.serial, exc)


# --------------------------------------------------------------------------- #
# Рабочая область и поиск рекламы
# --------------------------------------------------------------------------- #


async def measure_workspace(device: Device, *, timeout: float = 15.0) -> Workspace | None:
    """Определить рабочую область по границам ``top_bar_compose_root`` и ленты.

    Сначала дожидаемся появления ленты Discover и верхней панели: после холодного
    старта приложение короткое время показывает только поисковую строку, а
    ``recycler_view`` и ``top_bar_compose_root`` инфлейтятся чуть позже — без
    ожидания можно снять дамп раньше времени.
    """
    try:
        await device.wait_for(Selector.id(RECYCLER_ID), timeout=timeout)
        await device.wait_for(Selector.id(TOP_BAR_ID, match="contains"), timeout=timeout)
    except WaitTimeout:
        logger.error("[%s] лента Discover не появилась за %.0fс", device.serial, timeout)
        return None
    tree = await device.dump()
    top_bar = tree.find(Selector.id(TOP_BAR_ID, match="contains"))
    recycler = tree.find(Selector.id(RECYCLER_ID))
    if top_bar is None or top_bar.bounds is None:
        logger.error("[%s] не найден %s", device.serial, TOP_BAR_ID)
        return None
    if recycler is None or recycler.bounds is None:
        logger.error("[%s] не найдена лента %s", device.serial, RECYCLER_ID)
        return None
    ws = Workspace(
        top=top_bar.bounds.bottom,
        bottom=recycler.bounds.bottom,
        left=recycler.bounds.left,
        right=recycler.bounds.right,
    )
    logger.info("[%s] рабочая область: top=%d bottom=%d", device.serial, ws.top, ws.bottom)
    return ws if ws.height > 0 else None


def _is_chrome_open(tree: UiTree) -> bool:
    """Случайно ли открылся Chrome (по его ``toolbar_container``)."""
    return tree.find(Selector.id(CHROME_TOOLBAR_ID)) is not None


def _find_ad_card(tree: UiTree) -> UiNode | None:
    """Прямой дочерний элемент ленты с рекламой — за один проход.

    Находим ленту один раз и идём по её ПРЯМЫМ детям; первый, в поддереве которого
    встречается «Sponsored» (в ``text`` или ``contentDesc``), и есть рекламная
    карточка. Без повторного поиска ленты и без подъёма по родителям — вертикальный
    список, поэтому первый совпавший прямой ребёнок однозначен.
    """
    recycler = tree.find(Selector.id(RECYCLER_ID))
    if recycler is None:
        return None
    for child in recycler.children:
        for node in child.walk():
            if SPONSORED_TEXT in (node.text or "") or SPONSORED_TEXT in (node.content_desc or ""):
                return child
    return None


# --------------------------------------------------------------------------- #
# Позиционирование карточки и скрин
# --------------------------------------------------------------------------- #


async def _align_loop(
    device: Device,
    ws: Workspace,
    cfg: ScrapeConfig,
    pick_target,
) -> tuple[UiTree, UiNode, AdInfo] | None:
    """Подвести границу ``pick_target(info)`` рекламы к низу рабочей области.

    Замкнутый цикл: на каждой итерации заново измеряем карточку, считаем целевую
    границу и доскролливаем, пока зазор не станет в пределах допуска (или карточка
    не уедет/исчезнет).

    Returns:
        ``(tree, card, info)`` финального состояния, либо ``None`` если потеряли карточку.
    """
    last: tuple[UiTree, UiNode, AdInfo] | None = None
    for _ in range(cfg.align_max_iters):
        tree = await device.dump()
        card = _find_ad_card(tree)
        if card is None or card.bounds is None:
            return last
        info = parse_ad(card)
        last = (tree, card, info)
        target = pick_target(info)
        if target is None:
            return last
        delta = target - ws.bottom
        if delta > cfg.align_tolerance:
            # Цель заходит за низ области (обрезана) — тянем ВВЕРХ. Не меньше min_drag:
            # короткий свайп Android трактует как клик по рекламе.
            await scroll_by(device, ws, max(delta, cfg.min_drag), cfg)
        elif delta < -cfg.min_drag:
            # Большой зазор снизу — подтягиваем ВНИЗ ближе к низу области.
            await scroll_by(device, ws, delta, cfg)
        else:
            return last
    return last


async def align_ad(
    device: Device, ws: Workspace, cfg: ScrapeConfig
) -> tuple[UiTree, UiNode, AdInfo] | None:
    """Подвести НИЗ РЕКЛАМЫ (футер) к низу рабочей области — для общего скрина."""
    return await _align_loop(device, ws, cfg, lambda info: info.content_bottom)


async def align_image_bottom(
    device: Device, ws: Workspace, cfg: ScrapeConfig
) -> tuple[UiTree, UiNode, AdInfo] | None:
    """Подвести НИЗ КАРТИНКИ к низу рабочей области — для скрина длинной картинки."""
    return await _align_loop(device, ws, cfg, lambda info: info.image_bottom)


#: Классы видео-плеера в карточке.
VIDEO_CLASSES = ("VideoView", "SurfaceView", "TextureView")
#: resource_id-маркеры видео (видео-реклама — это WebView внутри video_frame).
VIDEO_ID_MARKERS = ("video_frame", "duration_badge", "webx_web_view")
#: Контейнеры карусели из нескольких картинок.
CAROUSEL_CLASSES = ("RecyclerView", "ViewPager")
#: Минимальная высота области картинки (px), иначе считаем, что картинки нет.
MIN_IMAGE_HEIGHT = 80


@dataclass(frozen=True, slots=True)
class AdInfo:
    """Извлечённые из карточки данные рекламы и геометрия для кропов/выравнивания.

    Attributes:
        text: Текст (заголовок) рекламы.
        channel: Наименование канала/рекламодателя.
        form: Форма — ``"image"`` / ``"carousel"`` / ``"video"`` (картинку не
            снимаем) / ``"google_app"`` (пропускаем целиком).
        top: Верх карточки (px).
        content_bottom: Низ рекламы = низ футера (где «Sponsored»). Всё ниже
            (оценка/рекомендации) в скрин не входит. Цель выравнивания, если
            реклама помещается целиком.
        card_box: Прямоугольник общего скрина карточки (без блока оценки).
        image_box: Прямоугольник картинки рекламы или ``None`` (видео/нет картинки).
        image_bottom: Низ картинки — цель выравнивания, если реклама не помещается.
    """

    text: str | None
    channel: str | None
    form: str
    top: int | None
    content_bottom: int | None
    card_box: tuple[int, int, int, int] | None
    image_box: tuple[int, int, int, int] | None
    image_bottom: int | None


def _walk(node: UiNode):
    """Обойти узел и всё его поддерево (pre-order)."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _footer_node(card: UiNode) -> UiNode | None:
    """Футер карточки — узел, среди прямых детей которого есть «Sponsored»."""
    for node in _walk(card):
        for child in node.children:
            if (child.text or "").strip() == SPONSORED_TEXT:
                return node
    return None


def _carousel_descendant_ids(card: UiNode) -> set[int]:
    """id() всех узлов, лежащих ВНУТРИ карусели (их текст — наложен на картинку)."""
    ids: set[int] = set()
    for node in _walk(card):
        if any(c in (node.class_name or "") for c in CAROUSEL_CLASSES):
            for desc in node.descendants():
                ids.add(id(desc))
    return ids


def _is_google_app_ad(card: UiNode) -> bool:
    """Ведёт ли реклама на Google app (такие пропускаем целиком).

    TODO: маркер уточняется на реальных примерах (см. режим сбора структур).
    Пока всегда ``False`` — ни одно объявление не помечается как google-app.
    """
    return False


def detect_form(card: UiNode) -> str:
    """Определить форму рекламы по структуре карточки."""
    if _is_google_app_ad(card):
        return "google_app"
    for node in _walk(card):
        rid = node.resource_id or ""
        cls = node.class_name or ""
        desc = node.content_desc or ""
        if any(m in rid for m in VIDEO_ID_MARKERS):
            return "video"
        if any(v in cls for v in VIDEO_CLASSES):
            return "video"
        if desc.startswith("Video "):
            return "video"
    for node in _walk(card):
        if any(c in (node.class_name or "") for c in CAROUSEL_CLASSES):
            return "carousel"
    return "image"


def parse_ad(card: UiNode) -> AdInfo:
    """Извлечь из карточки текст, канал, форму и геометрию кропов.

    * Канал — текстовый сосед «Sponsored» в футере.
    * Низ рекламы — низ футера: блок оценки/рекомендаций ниже него не учитываем.
    * Заголовок — самый длинный текстовый узел ВНЕ футера и ВНЕ карусели (текст,
      наложенный на картинку карусели, заголовком не считаем).
    * Картинка — область карточки выше заголовка; если отдельного заголовка нет
      (текст внутри карусели) — до низа футера, чтобы нижний текст вошёл в кадр.
    """
    form = detect_form(card)
    footer = _footer_node(card)
    footer_ids = {id(n) for n in _walk(footer)} if footer is not None else set()
    carousel_ids = _carousel_descendant_ids(card)

    channel: str | None = None
    if footer is not None:
        for child in footer.children:
            text = (child.text or "").strip()
            if text and text != SPONSORED_TEXT:
                channel = text
                break

    def _longest_text(skip_carousel: bool) -> UiNode | None:
        best_node, best_len = None, -1
        for node in _walk(card):
            text = (node.text or "").strip()
            if not text or text in (SPONSORED_TEXT, channel):
                continue
            if id(node) in footer_ids or (skip_carousel and id(node) in carousel_ids):
                continue
            if len(text) > best_len:
                best_node, best_len = node, len(text)
        return best_node

    # Отдельный заголовок — вне карусели; если такого нет (карусель с текстом
    # внутри), берём самый длинный текст вообще (имя товара) только ради text.
    headline = _longest_text(skip_carousel=True)
    text_node = headline or _longest_text(skip_carousel=False)

    if card.bounds is None:
        return AdInfo(
            text=text_node.text.strip() if text_node and text_node.text else None,
            channel=channel,
            form=form,
            top=None,
            content_bottom=None,
            card_box=None,
            image_box=None,
            image_bottom=None,
        )

    top = card.bounds.top
    content_bottom = (
        footer.bounds.bottom
        if footer is not None and footer.bounds is not None
        else card.bounds.bottom
    )
    card_box = (card.bounds.left, top, card.bounds.right, content_bottom)

    # Низ картинки: до верха отдельного заголовка, иначе до верха футера
    # (тогда нижний текст карусели входит в кадр — «для красоты»).
    if headline is not None and headline.bounds is not None:
        image_bottom = headline.bounds.top
    elif footer is not None and footer.bounds is not None:
        image_bottom = footer.bounds.top
    else:
        image_bottom = content_bottom

    image_box: tuple[int, int, int, int] | None = None
    if form != "video" and image_bottom - top >= MIN_IMAGE_HEIGHT:
        image_box = (card.bounds.left, top, card.bounds.right, image_bottom)

    return AdInfo(
        text=text_node.text.strip() if text_node and text_node.text else None,
        channel=channel,
        form=form,
        top=top,
        content_bottom=content_bottom,
        card_box=card_box,
        image_box=image_box,
        image_bottom=image_bottom,
    )


def _node_to_dict(node: UiNode) -> dict:
    """Сериализовать узел и всё его поддерево в JSON-совместимый словарь."""
    bounds = None
    if node.bounds is not None:
        b = node.bounds
        bounds = {"left": b.left, "top": b.top, "right": b.right, "bottom": b.bottom}
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


def save_node_structure(node: UiNode, path: Path) -> None:
    """Сохранить структуру (UI-поддерево) узла в JSON рядом со скрином."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_node_to_dict(node), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clamp_box(
    box: tuple[int, int, int, int], img: Image.Image
) -> tuple[int, int, int, int] | None:
    """Прижать прямоугольник к границам изображения; ``None`` если он вырожден."""
    left, top, right, bottom = box
    clamped = (max(0, left), max(0, top), min(img.width, right), min(img.height, bottom))
    if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
        return None
    return clamped


async def _screenshot(device: Device) -> Image.Image:
    """Снять скриншот экрана (PNG) с паузой под рейт-лимит и дорисовку."""
    await device.sleep(0.8)  # уважаем рейт-лимит скриншота (~1/с) и даём дорисоваться
    return Image.open(BytesIO(await device.screenshot(format="png")))


async def capture_ad(
    device: Device, card: UiNode, info: AdInfo, path: Path, ws: Workspace, cfg: ScrapeConfig
) -> AdInfo | None:
    """Сохранить артефакты рекламы рядом с ``path``.

    Порядок: общий скрин (даже если картинка не помещается) → текст и канал → если
    верх картинки выше верха рабочей области, подтянуть НИЗ КАРТИНКИ к низу области
    и снять картинку отдельным скрином. Складывает:

    * ``<stem>.png`` — общий скрин карточки без блока оценки (кроме Google app);
    * ``<stem>.json`` — структура карточки;
    * ``<stem>.meta.json`` — текст, канал и форма рекламы;
    * ``<stem>.image.png`` — скрин картинки рекламы (кроме видео и если область есть).

    Returns:
        ``info``, либо ``None`` если у карточки нет bounds.
    """
    if info.form == "google_app":
        logger.info("[%s] реклама на Google app — пропускаю", device.serial)
        return info
    if card.bounds is None or info.card_box is None or info.top is None:
        logger.warning("[%s] у карточки нет bounds — пропуск", device.serial)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Общий скрин — в любом случае, даже если картинка не влезла целиком.
    img = await _screenshot(device)
    card_box = _clamp_box(info.card_box, img)
    if card_box is not None:
        img.crop(card_box).save(path)

    # 2. Текст, канал, форма (из структуры) + сама структура.
    save_node_structure(card, path.with_suffix(".json"))
    path.with_name(path.stem + ".meta.json").write_text(
        json.dumps(
            {"text": info.text, "channel": info.channel, "form": info.form},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. Картинка рекламы (кроме видео).
    image_saved = False
    if info.form != "video" and info.image_box is not None:
        image_path = path.with_name(path.stem + ".image.png")
        if info.top > ws.top:
            # Картинка целиком в рабочей области — кропим из общего скрина.
            image_box = _clamp_box(info.image_box, img)
            if image_box is not None:
                img.crop(image_box).save(image_path)
                image_saved = True
        else:
            # Верх картинки выше области — подтягиваем НИЗ картинки к низу области
            # и снимаем картинку отдельным скрином.
            realigned = await align_image_bottom(device, ws, cfg)
            if realigned is not None:
                _tree, _card2, info2 = realigned
                if info2.image_box is not None:
                    img2 = await _screenshot(device)
                    image_box = _clamp_box(info2.image_box, img2)
                    if image_box is not None:
                        img2.crop(image_box).save(image_path)
                        image_saved = True

    logger.info(
        "[%s] реклама [%s] канал=%r картинка=%s -> %s",
        device.serial,
        info.form,
        info.channel,
        "да" if image_saved else "нет",
        path.name,
    )
    return info


# --------------------------------------------------------------------------- #
# Сессия скроллинга и полный сценарий
# --------------------------------------------------------------------------- #


async def scroll_session(device: Device, ws: Workspace, cfg: ScrapeConfig, cycle: int) -> int:
    """Пролистать ленту на ``cfg.num_swipes`` свайпов, собирая рекламу.

    Returns:
        Сколько реклам сохранено за сессию.
    """
    swipes_done = 0
    ads_found = 0
    chrome_recoveries = 0
    while swipes_done < cfg.num_swipes:
        tree = await device.dump()

        # Случайно открылся Chrome — жмём back и продолжаем листать (свайп не тратим).
        if _is_chrome_open(tree):
            chrome_recoveries += 1
            if chrome_recoveries > cfg.max_chrome_recoveries:
                logger.warning("[%s] Chrome не закрывается — прерываю сессию", device.serial)
                break
            logger.info("[%s] открылся Chrome — жму back", device.serial)
            await device.global_action("back")
            await device.sleep(cfg.settle)
            continue
        chrome_recoveries = 0

        if _find_ad_card(tree) is not None:
            aligned = await align_ad(device, ws, cfg)
            if aligned is not None:
                _final_tree, card, info = aligned
                ads_found += 1
                path = cfg.out_dir / device.serial / f"cycle{cycle}_ad{ads_found:03d}.png"
                await capture_ad(device, card, info, path, ws, cfg)
            # Несколько свайпов, чтобы не наткнуться на эту же рекламу снова
            # (после выравнивания картинки могли откатиться назад).
            for _ in range(cfg.post_ad_swipes):
                await swipe_forward(device, ws, cfg)
            swipes_done += cfg.post_ad_swipes
        else:
            await swipe_forward(device, ws, cfg)
            swipes_done += 1
    logger.info(
        "[%s] цикл %d: %d свайпов, найдено реклам: %d",
        device.serial,
        cycle,
        swipes_done,
        ads_found,
    )
    return ads_found


async def scrape_discover_ads(device: Device, cfg: ScrapeConfig | None = None) -> int:
    """Полный сценарий: повторять «открыть → листать → убить» ``cfg.cycles`` раз.

    Returns:
        Суммарное число сохранённых реклам по всем циклам.
    """

    cfg = cfg or ScrapeConfig()
    await prepare_device(device)
    total_ads = 0
    cycle = 0
    while cfg.cycles is None or cycle < cfg.cycles:
        cycle += 1
        try:
            await open_app(device, package=GOOGLE_APP_PACKAGE, ready_timeout=cfg.ready_timeout)
            ws = await measure_workspace(device, timeout=cfg.ready_timeout)
            if ws is None:
                logger.error("[%s] цикл %d: не удалось определить область", device.serial, cycle)
            else:
                total_ads += await scroll_session(device, ws, cfg, cycle)
                # Обновляем ленту перед закрытием, чтобы при следующем открытии
                # подгрузились свежие объявления.
                await refresh_feed(device, ws, cfg)
        except AxonError as exc:
            logger.error("[%s] цикл %d: ошибка axonctl: %s", device.serial, cycle, exc)
        except Exception as exc:
            logger.error("[%s] цикл %d: сбой: %s", device.serial, cycle, exc)
        finally:
            await close_app(device, package=GOOGLE_APP_PACKAGE)
    return total_ads


async def main() -> None:
    """Запустить скрейпинг на всех подключённых устройствах."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = ScrapeConfig()
    async with FleetController.from_config("fleet.toml") as fleet:
        results = await fleet.run(lambda device: scrape_discover_ads(device, cfg))

    for serial, outcome in results.items():
        if outcome.ok:
            logger.info("[%s] готово, реклам собрано: %s", serial, outcome.value)
        else:
            logger.error("[%s] прогон упал: %s", serial, outcome.error)


if __name__ == "__main__":
    asyncio.run(main())
