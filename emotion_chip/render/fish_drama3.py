"""fish_drama3 dialect entry point (tables are the source of truth)."""
from . import render as _render


def render(text_or_score, options=None, data=None):
    return _render(text_or_score, "fish_drama3", options, data)
