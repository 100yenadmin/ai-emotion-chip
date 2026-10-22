"""The one rule that removes authored material: with sounds off, affect sounds are dropped."""
from dataclasses import replace

from .model import GAP, Dropped, Event, Sentence, Text, option


def apply_policy(score, data, options=None):
    sounds = option(options, "sounds", True)
    if not isinstance(sounds, bool):
        raise ValueError("sounds must be true or false")
    if sounds:
        return score, []
    # Sounds off is the user's switch (/chip quiet). Cues, spans, pauses and breaths stay.
    dropped = []
    sentences = []
    for sentence in score.sentences:
        kept = []
        for token in sentence.tokens:
            if isinstance(token, Event) and data.sounds["events"][token.id]["kind"] == "affect":
                dropped.append(Dropped("event", token.id, "sounds off"))
                kept.append(Text(GAP))
                continue
            kept.append(token)
        sentences.append(Sentence(kept))
    return replace(score, sentences=sentences), dropped
