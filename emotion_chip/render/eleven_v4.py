"""eleven_v4 dialect entry point (tables are the source of truth)."""
from . import render as _render


def render(text_or_score, options=None, data=None):
    return _render(text_or_score, "eleven_v4", options, data)
