from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from duke.nlu.temporal import parse_french_temporal

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 5, 7, 14, 30, tzinfo=PARIS)


def test_ce_matin() -> None:
    out = parse_french_temporal("j'ai pulvérisé ce matin", now=NOW)
    assert out.started_at is not None
    assert out.started_at.date() == NOW.date()
    assert out.started_at.hour == 6
    assert out.stopped_at is not None
    assert out.stopped_at.hour == 12


def test_hier() -> None:
    out = parse_french_temporal("intervention hier", now=NOW)
    assert out.started_at is not None
    assert out.started_at.date() == (NOW.date() - timedelta(days=1))


def test_avant_hier() -> None:
    out = parse_french_temporal("on a labouré avant-hier", now=NOW)
    assert out.started_at is not None
    assert out.started_at.date() == (NOW.date() - timedelta(days=2))


def test_pendant_1h30() -> None:
    out = parse_french_temporal("traitement ce matin pendant 1h30", now=NOW)
    assert out.working_duration == timedelta(hours=1, minutes=30)


def test_pendant_2_heures() -> None:
    out = parse_french_temporal("pulvérisation hier matin pendant 2 heures", now=NOW)
    assert out.working_duration == timedelta(hours=2)


def test_30_minutes() -> None:
    out = parse_french_temporal("contrôle pendant 30 minutes", now=NOW)
    assert out.working_duration == timedelta(minutes=30)


def test_a_14h30() -> None:
    out = parse_french_temporal("on a démarré aujourd'hui à 14h30", now=NOW)
    assert out.started_at is not None
    assert out.started_at.hour == 14
    assert out.started_at.minute == 30


def test_no_temporal_returns_empty() -> None:
    out = parse_french_temporal("pulvérisation de Karaté Zeon", now=NOW)
    assert out.started_at is None
    assert out.stopped_at is None
    assert out.working_duration is None


def test_hier_apres_midi() -> None:
    out = parse_french_temporal("hier après-midi", now=NOW)
    assert out.started_at is not None
    assert out.started_at.date() == NOW.date() - timedelta(days=1)
    assert out.started_at.hour == 13
