"""Render the score using data tables; no inference or vendor dependencies."""
import re

from ..data import load_data
from ..model import Cue, DeliveryPlan, Dropped, Event, Rendered, Segment, Span, Speaker, Text, option
from ..policy import apply_policy
from ..score import parse


class Dialect(str):
    """A string dialect identifier carrying routing notes and model variant."""
    def __new__(cls, name, variant=None, notes=()):
        value = super().__new__(cls, name)
        value.variant = variant
        value.notes = list(notes)
        return value


def dialect_for(backend, model):
    if backend == "fish":
        if model in {"s2.1-pro", "s2.1-pro-free", "s2-pro", "s1"}:
            return Dialect("fish_s2")
        if model in {"drama-3", "drama-3-preview"}:
            return Dialect("fish_drama3")
    if backend == "eleven":
        if model in {"eleven_v4", "eleven_v4_turbo", "eleven_v3", "eleven_v3_conversational"}:
            return Dialect("eleven_v4", model)
        if model in {"eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5"}:
            return Dialect("tagless")
    return Dialect("tagless", notes=[f"unknown backend/model: {backend}/{model}; using tagless"])


def _first_cue(score):
    return next((t for s in score.sentences for t in s.tokens if isinstance(t, Cue)), None)


def _group(cue, data):
    if cue is None:
        return None
    rows = [data.lookup(word) for word in cue.words]
    # Unknown words have neutral arousal; a tie always keeps the first part.
    winner = max(enumerate(rows), key=lambda pair: abs((pair[1] or {}).get("arousal_bucket", 0)))[1]
    return winner["group"] if winner else None


def _params(dialect, group, data, options):
    override = option(options, "params_from")
    if isinstance(override, dict):
        return dict(override)
    row = data.groups.get(group, {})
    if dialect.startswith("fish_"):
        overlay = option(options, "overlay", None) or {}
        offset = overlay.get("fish_speed_offset", 0.0)
        return {"speed": max(0.85, min(1.10, row.get("fish_speed", 1.0) + offset))}
    if dialect == "eleven_v4":
        return {"stability": max(0.0, min(1.0, option(options, "eleven_stability", 0.5) + row.get("el_stability_offset", 0.0))),
                "similarity_boost": option(options, "eleven_similarity", 0.75)}
    return {}


def render(text_or_score, dialect, options=None, data=None):
    data = data if data is not None else load_data()
    notes = list(getattr(dialect, "notes", []))
    model = option(options, "model") or getattr(dialect, "variant", None)
    if dialect not in data.dialects:
        notes.append(f"unknown dialect: {dialect}; using tagless")
        dialect = "tagless"
    name = str(dialect)
    table = data.dialects[name]
    original = parse(text_or_score) if isinstance(text_or_score, str) else text_or_score
    score, policy_drops = apply_policy(original, data, options)
    dropped = list(original.dropped) + policy_drops
    if score.header is not None:
        dropped.append(Dropped("header", score.header.raw, "not spoken"))
    # Params follow the first cue that survives policy, so a dropped forbidden cue never sets them.
    first = _first_cue(score)
    override = option(options, "params_from")
    group = override if isinstance(override, str) else _group(first, data)
    if isinstance(override, str) and group not in data.groups:
        raise ValueError(f"unknown params_from group: {group}")
    params = _params(name, group, data, options)
    segments = []
    output = []
    tagless = name in {"tagless", "prose"}
    # Each segment carries its own group and rung (the plan feeds non-voice channels); params stay one set per call.
    # An override names the group from the start; a rung is only ever taken from a cue already seen.
    seg_group, seg_rung = (group if isinstance(override, str) else None), "plain"
    for index, sentence in enumerate(score.sentences):
        lead = next((t for t in sentence.tokens if isinstance(t, Cue)), None)
        if lead is not None:
            seg_group, seg_rung = _group(lead, data), lead.rung
        chunks = []
        plain = []
        cues, spans, events = [], [], []
        for token in sentence.tokens:
            if isinstance(token, Text):
                chunks.append(token.s)
                plain.append(token.s)
            elif isinstance(token, Cue):
                cues.append(token)
                if tagless:
                    dropped.append(Dropped("cue", token.raw, "unsupported"))
                else:
                    chunks.append("[" + token.raw + "]")
            elif isinstance(token, Span):
                spans.append(token)
                plain.append(token.text)
                template = table["spans"].get(token.op)
                if not template:
                    chunks.append(token.text)
                    dropped.append(Dropped("span", token.op, "unsupported"))
                elif template == "uppercase":
                    chunks.append(token.text.upper())
                else:
                    chunks.append(template.format(text=token.text))
                    if name == "fish_s2":
                        notes.append("span scope lost (sentence-level)")
            elif isinstance(token, Event):
                events.append(token)
                literal = table["events"].get(token.id)
                if literal:
                    chunks.append(literal)
                else:
                    dropped.append(Dropped("event", token.id, "unsupported"))
            elif isinstance(token, Speaker):
                if table.get("speaker") and option(options, "allow_speakers", False):
                    chunks.append(table["speaker"].format(id=token.id))
                else:
                    dropped.append(Dropped("speaker", token.id, "unsupported"))
        count = sum(isinstance(t, Cue) for t in original.sentences[index].tokens)
        if count > 3:
            notes.append(f"sentence {index + 1} has {count} cues (Fish guidance: ≤3)")
        # Collapse gaps left by dropped tokens; the agent's own bracket text stays byte-for-byte.
        rendered = re.sub(r"(\[[^\[\]]*\])|[ \t]{2,}", lambda m: m[1] or " ", "".join(chunks)).strip()
        if name == "eleven_v4":
            rendered = re.sub(r"(?<!\.)\.\.(?!\.)", "…", rendered)
        if rendered:
            output.append(rendered)
        segments.append(Segment(index, "".join(plain).strip(), cues, spans, events, seg_group, seg_rung, dict(params)))
    text = " ".join(output)
    if name == "eleven_v4":
        cap = table["variants"].get(model, 10000)
        if len(text) > cap:
            notes.append(f"character cap exceeded ({len(text)} > {cap})")
    return Rendered(text, params, dropped, notes, DeliveryPlan(segments), name, table["dialect_version"], group)
