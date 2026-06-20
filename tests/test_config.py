"""Тесты загрузки и переопределения конфигурации."""

from pathlib import Path

from googleadsparser.config import FleetOptions, ScrapeConfig, load_config


def test_defaults() -> None:
    cfg = ScrapeConfig()
    assert cfg.swipes == 25
    assert cfg.output_dir == Path("ads")
    assert cfg.error_dir == Path("errors")
    assert cfg.save_ui_html is True
    assert cfg.page_info_attempts == 2
    assert cfg.device_names == {}


def test_scrape_from_section_overrides_and_types() -> None:
    cfg = ScrapeConfig.from_section(
        {
            "swipes": 40,
            "output_dir": "out",
            "error_dir": "errs",
            "save_ui_html": False,
            "device_names": {"276bcca9": "phone1"},
            "unknown_key": "ignored",
        }
    )
    assert cfg.swipes == 40
    assert cfg.output_dir == Path("out")
    assert cfg.error_dir == Path("errs")
    assert cfg.save_ui_html is False
    assert cfg.device_names == {"276bcca9": "phone1"}


def test_fleet_from_section() -> None:
    fleet = FleetOptions.from_section({"concurrency": 4, "devices": ["a", "b"]})
    assert fleet.concurrency == 4
    assert fleet.devices == ("a", "b")


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    scrape, fleet = load_config(tmp_path / "нет-такого.toml")
    assert scrape == ScrapeConfig()
    assert fleet == FleetOptions()


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        '[scrape]\nswipes = 7\n[scrape.device_names]\n"s1" = "p1"\n[fleet]\ndevices = ["s1"]\n',
        encoding="utf-8",
    )
    scrape, fleet = load_config(path)
    assert scrape.swipes == 7
    assert scrape.device_names == {"s1": "p1"}
    assert fleet.devices == ("s1",)
