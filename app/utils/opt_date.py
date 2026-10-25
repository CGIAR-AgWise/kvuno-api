"""
The optimal sowing date (`opt_date`) is a day of the year, with no year.

AgWise files store it as day-month text — "30-Nov", "05-Jan" — because it is
a recommendation that recurs every season (a season can also run from
November into January, so there is no single year to attach). The data is
not truncated; inventing a year would add information it does not have.

For machines, responses also carry `opt_month_day` in ISO 8601's
month-day form, "--11-30" (XML Schema gMonthDay): unambiguous, sortable,
parseable without knowing the English month abbreviations.
"""
from __future__ import annotations

import re

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_MONTH_NUMBER = {name.lower(): i for i, name in enumerate(MONTHS, start=1)}

_STORED = re.compile(r'^(\d{1,2})-([A-Za-z]{3})$')            # 30-Nov
_MONTH_DAY = re.compile(r'^-{0,2}(\d{2})-(\d{2})$')           # --11-30 or 11-30
_FULL_DATE = re.compile(r'^\d{4}-(\d{2})-(\d{2})$')           # 2026-11-30 (year ignored)


def _valid(month: int, day: int) -> bool:
    # 29 Feb is a valid day of the year (leap years recur too).
    days = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return 1 <= month <= 12 and 1 <= day <= days[month - 1]


def month_day(stored: str | None) -> str | None:
    """'30-Nov' -> '--11-30'; None when the value is not a day-month."""
    if not stored:
        return None
    m = _STORED.match(stored.strip())
    if not m:
        return None
    day, month = int(m.group(1)), _MONTH_NUMBER.get(m.group(2).lower())
    if month is None or not _valid(month, day):
        return None
    return f'--{month:02d}-{day:02d}'


def to_stored(value: str) -> str:
    """A filter value in any accepted form, as stored: '30-Nov'.

    Accepts the stored form (any case, '5-Jan' or '05-Jan'), ISO month-day
    ('--11-30' or '11-30'), or a full date ('2026-11-30', year ignored —
    the data has none). Raises ValueError otherwise.
    """
    text = value.strip()
    m = _STORED.match(text)
    if m:
        day, month = int(m.group(1)), _MONTH_NUMBER.get(m.group(2).lower())
    else:
        m = _MONTH_DAY.match(text) or _FULL_DATE.match(text)
        if not m:
            raise ValueError("Date must be a day and month: '30-Nov', '--11-30' or '2026-11-30' (year ignored).")
        month, day = int(m.group(1)), int(m.group(2))
    if month is None or not _valid(month, day):
        raise ValueError(f"'{value}' is not a valid day of the year.")
    return f'{day:02d}-{MONTHS[month - 1]}'
