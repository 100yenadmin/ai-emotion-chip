"""Fish's own paren adapter, and a render that settles against it."""
from dataclasses import replace
import importlib
import re

from ..model import Cue, option
from ..render import render
from ..score import parse


def fish_adapter(fish):
    """fish-audio's adapt_tags, imported from the package of the loaded Fish provider."""
    package = type(fish).__module__.rsplit(".", 1)[0]
    return importlib.import_module(package + ".tags").adapt_tags


# A (cue) paren that only bracket tokens precede in its sentence ("[sad] (sobbing) Sorry."). Fish leaves it alone, and
# what each model does with it is not documented, so the chip converts it too and policy sees it.
_LEADING = re.compile(r"((?:^|[.!?。！？\n])(?:\s*(?:\[\[[^\[\]]*\]\]|\[[^\[\]]*\]))+\s*)(\([^()\n]{1,64}\))")


def adapt_all(text, adapt):
    """Fish's S2 paren adaptation, plus parens that lead their sentence after bracket tokens, until nothing changes."""
    for _ in range(text.count("(") + 1):
        new = _LEADING.sub(lambda m: m[1] + adapt(m[2], "S2"), adapt(text, "S2"))
        if new == text:
            break
        text = new
    return text


def _cue_words(text, data):
    return [w for s in parse(text, data).sentences for t in s.tokens if isinstance(t, Cue) for w in t.words]


def settled_render(score, dialect, options, data, adapt=None):
    """Render, then render again while Fish's adapter would still turn a (cue) paren into a tag.

    Fish adapts the chip's output again, and a paren becomes sentence-initial once the token before it is dropped
    ("Done. [hysterical] (sobbing) Sorry."). Each pass puts those new tags through the same policy (forbidden
    words, sound caps), with any carried policy state restored to where this call started. Later passes keep the
    params group of the first pass that has one, so settling only re-applies policy. Each change turns at least one
    paren into a tag, so the loop ends within one pass per paren. Returns the render and the cue words those parens
    added.
    """
    state = option(options, "policy_state")
    start = dict(state) if isinstance(state, dict) else None
    rendered = render(score, dialect, options, data)
    if adapt is None:
        return rendered, []
    # A breath the dialect cannot voice leaves the text in the first pass, so later passes cannot see it.
    first = dict(state) if start is not None else None
    dropped, notes, added = list(rendered.dropped), list(rendered.notes), []
    later = dict(options)
    for _ in range(rendered.text.count("(") + 1):
        if later.get("params_from") is None and rendered.group is not None:
            later["params_from"] = rendered.group
        try:
            adapted = adapt_all(rendered.text, adapt)
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
        rendered = render(parse(adapted, data), dialect, later, data)
        dropped += rendered.dropped
        notes += [n for n in rendered.notes if n not in notes]
        if first is not None:
            state["since_breath"] = first.get("since_breath", state.get("since_breath"))
    return replace(rendered, dropped=dropped, notes=notes), added
