"""Селекторы элементов интерфейса Google-приложения, Chrome и плеера."""

from axonctl import Selector


class GoogleSelectors:
    """Селекторы ключевых элементов интерфейса приложения Google и Chrome.

    Attributes:
        ROOT: Корневой контейнер приложения — его исчезновение означает, что
            приложение закрыто.
        FEED: Лента Discover (``RecyclerView``), которую листаем и в которой ищем рекламу.
        TOP_BAR: Верхняя панель — её низ задаёт верхнюю границу рабочей области.
        SEARCH: Поисковая строка — появляется раньше ленты и служит маркером готовности.
        SPONSORED: Пометка рекламы в карточке.
        CHROME_TOOL_BAR: Тулбар Chrome Custom Tab.
        CHROME_PROGRESS_BAR: Индикатор загрузки страницы в Chrome.
        CHROME_CLOSE: Кнопка закрытия (крестик) в Chrome Custom Tab / плеере.
        SHARE_LINK: Кнопка «Share link» в тулбаре Chrome.
        SHARE_PREVIEW_TEXT: Текст превью в системном диалоге шеринга (содержит ссылку).
        CHROME_URL_BAR: Адресная строка Chrome — тап открывает информацию о странице.
        PAGE_INFO_TRUNCATED_URL: Усечённый домен в попапе page info (тап раскрывает URL).
        PAGE_INFO_URL: Полный URL лендинга в попапе page info.
        PLAYER_CONTAINER: Контейнер видео-плеера (внутри WebView рекламы-видео).
        VIDEO_SHARE_URL: Поле со ссылкой на видео в диалоге «Поделиться» плеера.
        EDIT_TEXT: Любое редактируемое поле (для вставки буфера и чтения).
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
    #: Усечённый домен в попапе page info — тап по нему раскрывает полный URL.
    PAGE_INFO_TRUNCATED_URL = Selector.id("com.android.chrome:id/page_info_truncated_url")
    #: Полный URL лендинга в попапе page info (появляется после тапа по усечённому).
    PAGE_INFO_URL = Selector.id("com.android.chrome:id/page_info_url")
    #: Контейнер видео-плеера (внутри WebView рекламы-видео).
    PLAYER_CONTAINER = Selector.id("playerContainer")
    #: Поле со ссылкой на видео в диалоге «Поделиться» плеера.
    VIDEO_SHARE_URL = Selector.id("unified-share-url-input:0")
    #: Любое редактируемое поле (для вставки буфера и чтения).
    EDIT_TEXT = Selector.cls("android.widget.EditText")
