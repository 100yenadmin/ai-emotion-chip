"""fish_s2 dialect entry point (tables are the source of truth)."""
from . import render as _render


def render(text_or_score, options=None, data=None):
    return _render(text_or_score, "fish_s2", options, data)
