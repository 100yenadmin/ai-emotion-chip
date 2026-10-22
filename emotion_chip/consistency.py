"""Detect emotion words absent from the authored score, via dialect inverses."""
import re

from .model import Cue, Finding
from .score import cue, header_cue


def check(score, rendered, data):
    inverse = data.dialects[rendered.dialect]["inverse"]
    authored = {word for sentence in score.sentences for token in sentence.tokens
                if isinstance(token, Cue) for word in token.words}
    opener = header_cue(score.header)
    authored |= set(opener.words) if opener else set()
    # Authorship is checked against the agent's raw text too, so a parser rewrite cannot vouch for itself.
    raw_tokens = set(re.findall(r"[a-z]+", score.source.lower()))
    findings = []
    for match in re.finditer(r"\[[^\[\]]+\]", rendered.text):
        literal = match[0]
        if literal in inverse:
            continue
        for word in cue(literal[1:-1].strip()).words:
            if word not in authored:
                findings.append(Finding("added", word, "not present in authored Cue.words"))
            elif not set(re.findall(r"[a-z]+", word)) <= raw_tokens:
                findings.append(Finding("added", word, "not present in the agent's raw text"))
    return findings
