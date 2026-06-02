"""Тесты валидации :class:`SchedulerSettings` (фаза 8).

Чистая pydantic-валидация — без event-loop и без БД, поэтому здесь
нет ``pytestmark = pytest.mark.asyncio``.
"""

from __future__ import annotations

import os

import pytest

from app.config import SchedulerSettings


def test_settings_normalize_mode_case_insensitive() -> None:
    """``CRON`` и ``Interval`` нормализуются в lower-case."""
    assert SchedulerSettings(mode="CRON").mode == "cron"
    assert SchedulerSettings(mode="Interval").mode == "interval"


def test_settings_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(mode="random")


def test_settings_rejects_invalid_cron_times() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(cron_times="0,24")  # без двоеточия
    with pytest.raises(ValueError):
        SchedulerSettings(cron_times="24:00")  # час вне 0..23
    with pytest.raises(ValueError):
        SchedulerSettings(cron_times="09:60")  # минута вне 0..59
    with pytest.raises(ValueError):
        SchedulerSettings(cron_times="not-a-time")
    with pytest.raises(ValueError):
        SchedulerSettings(cron_times="")


def test_settings_strips_and_normalizes_cron_times() -> None:
    """Пробелы, ведущие нули и пустые элементы вычищаются."""
    assert (
        SchedulerSettings(cron_times=" 0:0 , 6:30 ,12:00,18:5 ").cron_times
        == "00:00,06:30,12:00,18:05"
    )


def test_settings_cron_pairs_parses_into_tuples() -> None:
    """``cron_pairs()`` возвращает пары ``(hour, minute)``."""
    pairs = SchedulerSettings(cron_times="00:00,09:30,14:15").cron_pairs()
    assert pairs == ((0, 0), (9, 30), (14, 15))


def test_settings_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(interval_minutes=0)
    with pytest.raises(ValueError):
        SchedulerSettings(interval_minutes=-5)


def test_settings_defaults_match_architecture_spec(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дефолты должны совпадать с ``architecture.md`` §4.

    Тест изолируется от любого ``.env`` рядом с проектом и от
    ``SCHEDULER_*`` в окружении dev-машины — иначе локальные
    значения разработчика ломали бы проверку дефолтов.
    """
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("SCHEDULER_")]:
        monkeypatch.delenv(key, raising=False)
    s = SchedulerSettings()
    assert s.mode == "cron"
    assert s.cron_times == "00:00,06:00,12:00,18:00"
    assert s.interval_minutes == 30
    assert s.run_on_startup is True
