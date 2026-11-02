"""Crop type values: one spelling per crop, so 'Maize ' and 'maize' are one crop.

Stored lower-case with single spaces, at most MAX_LENGTH characters (the
column's width). Free text rather than a fixed list: the crops come from the
data, and a new one should not need a release.
"""
import re

MAX_LENGTH = 30
_ALLOWED = re.compile(r"^[a-z0-9][a-z0-9 ()'_-]*$")


def normalise(value) -> str | None:
    """A file cell as a crop type: lower-case, spaces collapsed, clipped. Never raises.

    Used for values read from a file, where one odd cell must not fail its batch.
    """
    if value is None:
        return None
    text = ' '.join(str(value).split()).lower()
    if not text or text in ('nan', 'none', 'na'):
        return None
    return text[:MAX_LENGTH]


def parse(raw) -> str | None:
    """The mapping form's crop type. '' or None clears it; raises ValueError if unusable."""
    text = normalise(raw)
    if text is None:
        return None
    if len(' '.join(str(raw).split())) > MAX_LENGTH:
        raise ValueError(f'Crop type is longer than {MAX_LENGTH} characters')
    if not _ALLOWED.match(text):
        raise ValueError('Crop type may only contain letters, digits, spaces and - _ ( ) \'')
    return text
