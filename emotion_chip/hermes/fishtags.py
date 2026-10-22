"""Fish's own paren adapter, and a render that settles against it."""
from dataclasses import replace
import importlib

from ..model import Cue, option
from ..render import render
from ..score import parse


def fish_adapter(fish):
    """fish-audio's adapt_tags, imported from the package of the loaded Fish provider."""
    package = type(fish).__module__.rsplit(".", 1)[0]
    return importlib.import_module(package + ".tags").adapt_tags


def _cue_words(text, data):
    return [w for s in parse(text, data).sentences for t in s.tokens if isinstance(t, Cue) for w in t.words]


def settled_render(score, dialect, options, data, adapt=None):
    """Render, then render again while Fish's adapter would still turn a (cue) paren into a tag.

    Fish adapts the chip's output again, and a paren becomes sentence-initial once the token before it is dropped
    ("Done. [hysterical] (sobbing) Sorry."). Each pass puts those new tags through the same policy (forbidden
    words, sound caps), with any carried policy state restored to where this call started. Returns the render and
    the cue words those parens added.
    """
    state = option(options, "policy_state")
    start = dict(state) if isinstance(state, dict) else None
    rendered = render(score, dialect, options, data)
    if adapt is None:
        return rendered, []
    dropped, notes, added = list(rendered.dropped), list(rendered.notes), []
    for _ in range(8):
        try:
            adapted = adapt(rendered.text, "S2")
        except Exception:
            notes.append("Fish adapt_tags failed after render; output not re-checked")
            break
        if adapted == rendered.text:
            break
        before = _cue_words(rendered.text, data)
        for word in _cue_words(adapted, data):
            if word in before:
                before.remove(word)
            else:
                added.append(word)
        if start is not None:
            state.clear()
            state.update(start)
        rendered = render(parse(adapted, data), dialect, options, data)
        dropped += rendered.dropped
        notes += [n for n in rendered.notes if n not in notes]
    return replace(rendered, dropped=dropped, notes=notes), added
