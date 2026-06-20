"""Структуры данных рекламы."""

from dataclasses import dataclass


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
        top: Верх карточки (px); если он ≤ верха рабочей области, картинка вылезает
            выше и её надо снимать отдельно (подтянув низ к низу области).
        media_bottom: Низ медиа-области (верх заголовка/футера) — цель выравнивания
            при отдельном скрине картинки, либо ``None``.
    """

    form: str
    text: str | None
    channel: str | None
    is_google_play: bool
    card_box: tuple[int, int, int, int] | None
    media_box: tuple[int, int, int, int] | None
    top: int | None
    media_bottom: int | None
