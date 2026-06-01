"""Тесты валидации :class:`SchedulerSettings` (фаза 8).

Чистая pydantic-валидация — без event-loop и без БД, поэтому здесь
нет ``pytestmark = pytest.mark.asyncio``.
"""

from __future__ import annotations

import pytest

from app.config import SchedulerSettings


def test_settings_normalize_mode_case_insensitive() -> None:
    """``CRON`` и ``Interval`` нормализуются в lower-case."""
    assert SchedulerSettings(mode="CRON").mode == "cron"
    assert SchedulerSettings(mode="Interval").mode == "interval"


def test_settings_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(mode="random")


def test_settings_rejects_invalid_cron_hours() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(cron_hours="0,24")  # 24 — вне 0..23
    with pytest.raises(ValueError):
        SchedulerSettings(cron_hours="not-a-number")
    with pytest.raises(ValueError):
        SchedulerSettings(cron_hours="")


def test_settings_strips_and_normalizes_cron_hours() -> None:
    """Пробелы и пустые элементы вычищаются, формат CSV нормализуется."""
    assert SchedulerSettings(cron_hours=" 0 , 6 ,12,18 ").cron_hours == "0,6,12,18"


def test_settings_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        SchedulerSettings(interval_minutes=0)
    with pytest.raises(ValueError):
        SchedulerSettings(interval_minutes=-5)


def test_settings_defaults_match_architecture_spec() -> None:
    """Дефолты должны совпадать с ``architecture.md`` §4."""
    s = SchedulerSettings()
    assert s.mode == "cron"
    assert s.cron_hours == "0,6,12,18"
    assert s.interval_minutes == 30
    assert s.run_on_startup is True
