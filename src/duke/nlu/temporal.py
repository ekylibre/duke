"""French temporal expression parser.

Returns a TemporalExtraction with optional started_at / stopped_at / working_duration.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

DEFAULT_TZ = ZoneInfo("Europe/Paris")


class TemporalExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    working_duration: timedelta | None = None


_DAYPART_RANGES: dict[str, tuple[time, time]] = {
    "matin": (time(6, 0), time(12, 0)),
    "midi": (time(12, 0), time(13, 0)),
    "apres-midi": (time(13, 0), time(18, 0)),
    "apres midi": (time(13, 0), time(18, 0)),
    "soir": (time(18, 0), time(21, 0)),
    "nuit": (time(21, 0), time(23, 59)),
}

_DAY_OFFSETS: dict[str, int] = {
    "aujourd'hui": 0,
    "aujourdhui": 0,
    "ce jour": 0,
    "hier": -1,
    "avant-hier": -2,
    "avant hier": -2,
    "demain": 1,
}

_RE_DAYPART = re.compile(
    r"(?P<prefix>\bce\s+|\bcet\s+|\bcette\s+|\bhier\s+|\bce\s+jour\s+|\baujourd'?hui\s+)?"
    r"(?P<part>matin|midi|apr[eè]s[\s-]+midi|aprem|soir|nuit)\b",
    re.IGNORECASE,
)

_RE_DAY_KEYWORD = re.compile(
    r"\b(aujourd'?hui|ce\s+jour|hier|avant[\s-]+hier|demain)\b",
    re.IGNORECASE,
)

_RE_DURATION_HM = re.compile(
    r"(?:pendant\s+|durant\s+|en\s+|dur[eé]e\s+(?:de\s+)?)?"
    r"(?P<hours>\d{1,2})\s*h(?:\s*(?P<minutes>\d{1,2}))?\b",
    re.IGNORECASE,
)

_RE_DURATION_HOURS = re.compile(
    r"\b(?:pendant|durant|en|dur[eé]e\s+de)\s+(?P<hours>\d{1,2})\s*heures?\b",
    re.IGNORECASE,
)

_RE_DURATION_MINUTES = re.compile(
    r"\b(?:pendant\s+|durant\s+)?(?P<minutes>\d{1,3})\s*(?:min(?:utes?)?\.?)\b",
    re.IGNORECASE,
)

_RE_AT_TIME = re.compile(
    r"\b(?:[aà]\s+)?(?P<hour>\d{1,2})\s*(?:h|:)\s*(?P<minute>\d{2})\b",
    re.IGNORECASE,
)

_RE_DATE_DMY = re.compile(
    r"\b(?P<d>\d{1,2})[/\-\s](?P<m>\d{1,2})(?:[/\-\s](?P<y>\d{2,4}))?\b",
)


def _normalize(text: str) -> str:
    return (
        text.replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
        .replace("ï", "i")
        .replace("ô", "o")
        .replace("û", "u")
    )


def _resolve_anchor_date(normalized: str, today: date) -> tuple[date, str]:
    """Return (anchor_date, hint) where hint indicates how the date was anchored."""
    m = _RE_DAY_KEYWORD.search(normalized)
    if m:
        keyword = re.sub(r"\s+", " ", m.group(1).lower())
        offset = _DAY_OFFSETS.get(keyword.replace("'", "'"))
        if offset is None:
            offset = _DAY_OFFSETS.get(keyword)
        if offset is not None:
            return today + timedelta(days=offset), "keyword"

    m = _RE_DATE_DMY.search(normalized)
    if m:
        d = int(m.group("d"))
        mo = int(m.group("m"))
        y = m.group("y")
        if y is None:
            year = today.year
        else:
            year = int(y)
            if year < 100:
                year += 2000
        try:
            return date(year, mo, d), "explicit_date"
        except ValueError:
            pass

    return today, "default_today"


def _extract_daypart(normalized: str) -> tuple[time, time] | None:
    m = _RE_DAYPART.search(normalized)
    if not m:
        return None
    part = re.sub(r"[\s-]+", " ", m.group("part").lower())
    if part == "aprem":
        part = "apres midi"
    return _DAYPART_RANGES.get(part)


def _extract_at_time(normalized: str) -> time | None:
    m = _RE_AT_TIME.search(normalized)
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


def _extract_duration(normalized: str) -> timedelta | None:
    m = _RE_DURATION_HOURS.search(normalized)
    if m:
        return timedelta(hours=int(m.group("hours")))

    m = _RE_DURATION_HM.search(normalized)
    if m and ("pendant" in normalized or "durant" in normalized or "duree" in normalized):
        hours = int(m.group("hours"))
        minutes = int(m.group("minutes") or 0)
        return timedelta(hours=hours, minutes=minutes)

    m = _RE_DURATION_MINUTES.search(normalized)
    if m:
        minutes = int(m.group("minutes"))
        if minutes > 0:
            return timedelta(minutes=minutes)
    return None


def parse_french_temporal(
    text: str,
    *,
    now: datetime | None = None,
    tz: ZoneInfo = DEFAULT_TZ,
) -> TemporalExtraction:
    """Parse a French sentence and extract started_at/stopped_at/working_duration.

    The parser is conservative: every field stays None unless it is unambiguously
    determined from the text. It is meant to feed a follow-up LLM extraction, which
    can refine the fields when needed.
    """
    if now is None:
        now = datetime.now(tz=tz)
    today = now.date()

    normalized = _normalize(text.lower())

    anchor_date, _ = _resolve_anchor_date(normalized, today)
    daypart = _extract_daypart(normalized)
    at_time = _extract_at_time(normalized)
    duration = _extract_duration(normalized)

    started_at: datetime | None = None
    stopped_at: datetime | None = None

    if at_time is not None:
        started_at = datetime.combine(anchor_date, at_time, tzinfo=tz)
    elif daypart is not None:
        started_at = datetime.combine(anchor_date, daypart[0], tzinfo=tz)
        stopped_at = datetime.combine(anchor_date, daypart[1], tzinfo=tz)
    elif _RE_DAY_KEYWORD.search(normalized) or _RE_DATE_DMY.search(normalized):
        started_at = datetime.combine(anchor_date, time(8, 0), tzinfo=tz)

    if duration is not None and started_at is not None and stopped_at is None:
        stopped_at = started_at + duration

    return TemporalExtraction(
        started_at=started_at,
        stopped_at=stopped_at,
        working_duration=duration,
    )
