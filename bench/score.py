"""Deterministic raw-score checks and uncertainty estimates; no semantic inference."""
import math
import re
import statistics

from emotion_chip.consistency import check
from emotion_chip.data import load_data
from emotion_chip.model import Cue, Event, Span
from emotion_chip.render import render
from emotion_chip.score import _events, _header_shaped, cue, parse


def wilson(successes, n, z=1.959963984540054):
    if n < 0 or not 0 <= successes <= n:
        raise ValueError("invalid binomial counts")
    if not n:
        return [None, None]
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def rate(values):
    values = list(values)
    count, n = sum(bool(v) for v in values), len(values)
    return {"count": count, "n": n, "value": count / n if n else None, "ci95": wilson(count, n)}


def difference(a, b):
    """Newcombe independent-proportions interval using Wilson component limits."""
    if not a["n"] or not b["n"]:
        return {"value": None, "ci95": [None, None]}
    p, q = a["value"], b["value"]
    lower = p - q - math.hypot(p - a["ci95"][0], b["ci95"][1] - q)
    upper = p - q + math.hypot(a["ci95"][1] - p, q - b["ci95"][0])
    return {"value": p - q, "ci95": [max(-1.0, lower), min(1.0, upper)]}


def median_summary(values):
    values = sorted(values)
    n, k, cumulative = len(values), 0, 0.0
    # Distribution-free sign/order-statistic interval; small samples have no finite interval.
    for i in range(n // 2):
        cumulative += math.comb(n, i) / 2 ** n
        if cumulative > 0.025:
            break
        k = i + 1
    return {"value": statistics.median(values) if n else None, "n": n,
            "ci95": [values[k - 1], values[n - k]] if k else [None, None]}


def authored_cues(score):
    cues = [t for s in score.sentences for t in s.tokens if isinstance(t, Cue)]
    if score.header:
        # The header grammar permits bare words after semicolons as well as register: word.
        cues += [cue(part.partition(":")[2] if ":" in part else part)
                 for part in score.header.raw.split(";") if part.strip()]
    return cues


def raw_features(reply, data=None):
    data = data or load_data()
    score = parse(reply)
    tokens = [t for s in score.sentences for t in s.tokens]
    events = {t.id for t in tokens if isinstance(t, Event)}
    # Inverse tables also cover angle-markup vendor spellings.
    whisper = any(isinstance(t, Span) and t.op == "whisper" for t in tokens)
    for table in data.dialects.values():
        for literal, inverse in table["inverse"].items():
            if literal.lower() in reply.lower():
                if inverse["kind"] == "event":
                    events.add(inverse["id"])
                whisper |= inverse["kind"] == "span" and inverse["id"] == "whisper"
    cues = authored_cues(score)
    words = [word for c in cues for word in c.words]
    whisper |= any(re.search(r"\bwhisper(?:s|ing)?\b", word) for word in words)
    whisper |= bool(re.search(r"\[\s*whisper\s*:", reply, re.I))
    rows = [data.lookup(word) or {} for word in words]
    playful = any(row.get("group") in {"amusement", "elation"} for row in rows)
    playful |= any(re.search(r"\b(?:playful|joking|teasing)\b", word) for word in words)
    # Buckets are attached to the authored vocabulary's group-mapping rows.
    high_positive = any(row.get("valence_bucket", 0) >= 1 and row.get("arousal_bucket", 0) >= 1 for row in rows)
    sentence_counts = [sum(isinstance(t, (Cue, Event, Span)) for t in s.tokens) for s in score.sentences]
    header_count = len(cues) - sum(isinstance(t, Cue) for t in tokens)
    if header_count:
        if not sentence_counts:
            sentence_counts = [0]
        sentence_counts[0] += header_count
    count = sum(sentence_counts)
    return {"affect_events": sorted(e for e in events if data.sounds["events"][e]["kind"] == "affect"),
            "events": sorted(events),
            "whisper": bool(whisper), "playful": bool(playful),
            "cue_count": count, "sentence_cue_counts": sentence_counts,
            "cues_per_sentence": count / max(1, len(score.sentences)),
            "any_cue": bool(count), "high_positive": high_positive}


def header_leak(text, data):
    """Whether rendered text still holds a register header in any form the parser accepts as one."""
    events = _events(data.sounds)
    return any(_header_shaped(m[1], events, data) for m in re.finditer(r"\[\[([^\[\]]*)\]\]", text))


def g3_checks(reply, data=None):
    data = data or load_data()
    original = parse(reply)
    rendered = render(original, "fish_s2", data=data)
    checks = [("register_leak", header_leak(rendered.text, data)),
              ("consistency", bool(check(original, rendered, data)))]
    return [name for name, fails in checks if fails]
