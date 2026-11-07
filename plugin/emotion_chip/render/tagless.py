"""tagless dialect entry point (tables are the source of truth)."""
from . import render as _render


def render(text_or_score, options=None, data=None):
    return _render(text_or_score, "tagless", options, data)
