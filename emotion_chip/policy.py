"""Only the explicitly specified cue/event limits remove authored material."""
from dataclasses import replace
import re

from .model import Cue, Dropped, Event, Header, Sentence, option
from .score import cue, header_cue


def apply_policy(score, data, options=None):
    limit = option(options, "sounds_per_turn", 1)
    if not isinstance(limit, int) or limit < 0:
        raise ValueError("sounds_per_turn must be a nonnegative integer")
    # The sound denylist plus every concept marked assistant_ok: false, so the data field is enforced on its own.
    words = sorted(set(data.sounds["forbidden_cue_words"])
                   | {row["id"] for row in data.emotions if row.get("assistant_ok") is False}, key=len, reverse=True)
    # Matched on the rung-stripped word, as a whole word: [very hysterical], [crying softly] and [loud-screaming]
    # drop. So do negated mentions ([firm without screaming]): TTS models do not reliably follow negation inside a
    # tag, and naming the sound primes it. The guide asks for the positive delivery instead ([firm]).
    forbidden = re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)
    dropped = []
    sentences = []
    affect_count = 0
    last_breath = -2
    for index, sentence in enumerate(score.sentences):
        kept = []
        for token in sentence.tokens:
            if isinstance(token, Cue):
                allowed = [p for p, w in zip(token.parts, token.words) if not forbidden.search(w)]
                if len(allowed) != len(token.parts):
                    dropped.append(Dropped("cue", token.raw, "assistant_ok:false"))
                    if allowed:
                        kept.append(cue(", ".join(allowed)))
                    continue
            if isinstance(token, Event):
                kind = data.sounds["events"][token.id]["kind"]
                if kind == "affect":
                    if affect_count >= limit:
                        dropped.append(Dropped("event", token.id, "sounds_per_turn"))
                        continue
                    affect_count += 1
                if token.id in {"breath", "breath:shaky"}:
                    if index <= last_breath + 1:
                        dropped.append(Dropped("event", token.id, "breath density"))
                        continue
                    last_breath = index
            kept.append(token)
        sentences.append(Sentence(kept))
    header = score.header
    opener = header_cue(header)
    if opener is not None:
        allowed = [p for p, w in zip(opener.parts, opener.words) if not forbidden.search(w)]
        if len(allowed) != len(opener.parts):
            dropped.append(Dropped("header", header.raw, "assistant_ok:false"))
            header = Header({"register": ", ".join(allowed)}, "register: " + ", ".join(allowed)) if allowed else None
    return replace(score, header=header, sentences=sentences), dropped

