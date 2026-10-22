"""The one rule that removes authored material: with sounds off, affect sounds are dropped."""
from dataclasses import replace
import re

from .model import GAP, Dropped, Event, Header, Sentence, Text, option
from .score import header_cue


def apply_policy(score, data, options=None):
    sounds = option(options, "sounds", True)
    if not isinstance(sounds, bool):
        raise ValueError("sounds must be true or false")
    if sounds:
        return score, []
    # Sounds off is the user's switch (/chip quiet). Cues, spans, pauses and breaths stay.
    events = data.sounds["events"]
    dropped = []
    sentences = []
    for sentence in score.sentences:
        kept = []
        for token in sentence.tokens:
            if isinstance(token, Event) and events[token.id]["kind"] == "affect":
                dropped.append(Dropped("event", token.id, "sounds off"))
                kept.append(Text(GAP))
                continue
            kept.append(token)
        sentences.append(Sentence(kept))
    # A sound named in the register header ([[laughing]]) is a sound too, not a delivery cue.
    names = {name for name, row in events.items() if row["kind"] == "affect"}
    names |= {alias for alias, target in data.sounds.get("aliases", {}).items() if target in names}
    header = score.header
    opener = header_cue(header)
    if opener is not None:
        allowed = [p for p, w in zip(opener.parts, opener.words) if re.sub(r"\s+", " ", w).lower() not in names]
        if len(allowed) != len(opener.parts):
            dropped.append(Dropped("header", header.raw, "sounds off"))
            header = Header({"register": ", ".join(allowed)}, "register: " + ", ".join(allowed)) if allowed else None
    return replace(score, header=header, sentences=sentences), dropped
