"""Тесты чистой логики разбора карточки (без устройства).

Используется лёгкий дубль узла :class:`FakeNode`, повторяющий нужный интерфейс
``UiNode`` (resource_id/class_name/content_desc/text/bounds/children/descendants).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from googleadsparser import GoogleParser
from googleadsparser.config import ScrapeConfig


@dataclass
class FakeBounds:
    left: int = 0
    top: int = 0
    right: int = 100
    bottom: int = 100


@dataclass
class FakeNode:
    text: str | None = None
    content_desc: str | None = None
    resource_id: str = ""
    class_name: str = ""
    bounds: FakeBounds | None = None
    children: list["FakeNode"] = field(default_factory=list)

    def descendants(self) -> Iterator["FakeNode"]:
        for child in self.children:
            yield child
            yield from child.descendants()


class FakeDevice:
    serial = "testdev"


@pytest.fixture
def parser(tmp_path: Path) -> GoogleParser:
    return GoogleParser(FakeDevice(), ScrapeConfig(output_dir=tmp_path, error_dir=tmp_path))


def _card(*children: FakeNode) -> FakeNode:
    return FakeNode(children=list(children))


def test_extract_url() -> None:
    assert GoogleParser._extract_url("текст https://a.b/c?x=1#z хвост") == "https://a.b/c?x=1#z"
    assert GoogleParser._extract_url("домен без схемы a.b") is None
    assert GoogleParser._extract_url("") is None


def test_detect_form_video(parser: GoogleParser) -> None:
    card = _card(FakeNode(resource_id="...:id/video_frame"))
    assert parser.detect_form(card) == "video"


def test_detect_form_video_by_desc(parser: GoogleParser) -> None:
    card = _card(FakeNode(content_desc="Video 0:16"))
    assert parser.detect_form(card) == "video"


def test_detect_form_carousel(parser: GoogleParser) -> None:
    card = _card(FakeNode(class_name="androidx.recyclerview.widget.RecyclerView"))
    assert parser.detect_form(card) == "carousel"


def test_detect_form_image(parser: GoogleParser) -> None:
    card = _card(FakeNode(text="Заголовок рекламы"))
    assert parser.detect_form(card) == "image"


def test_is_google_play(parser: GoogleParser) -> None:
    gp = _card(FakeNode(text="Color Block · Google Play: 4.3 ★"))
    assert parser._is_google_play(gp) is True
    assert parser._is_google_play(_card(FakeNode(text="N. Peal"))) is False


def test_rating_top(parser: GoogleParser) -> None:
    card = _card(
        FakeNode(text="creative"),
        FakeNode(
            content_desc="Is this a good recommendation for you?", bounds=FakeBounds(top=1664)
        ),
    )
    assert parser._rating_top(card) == 1664
    assert parser._rating_top(_card(FakeNode(text="нет оценки"))) is None
