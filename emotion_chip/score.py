"""A bracket scanner; vendor angle markup remains ordinary text."""
import re
from pathlib import Path
from functools import lru_cache

import yaml

from .model import Cue, Dropped, Event, Header, Score, Sentence, Span, Speaker, Text

BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
SPAN = re.compile(r"^(soft|stress|slow|fast|whisper|smiling|emphasis|forceful):\s*(.+)$", re.I | re.S)
SPAN_OPEN = re.compile(r"\[\s*(soft|stress|slow|fast|whisper|smiling|emphasis|forceful):\s*", re.I)
INNER = re.compile(r"(\[[^\[\]]*\])")
RUNG = re.compile(r"^(slightly|a little|somewhat|very|extremely|deeply|really)\s+", re.I)


@lru_cache(maxsize=1)
def _events():
    """Event ids, plus vendor spellings (laughs, sighing, long pause) mapped to them."""
    path = Path(__file__).with_name("data") / "sounds.yaml"
    sounds = yaml.safe_load(path.read_text())
    return {**{e: e for e in sounds["events"]}, **sounds.get("aliases", {})}


def _top_level(text):
    """Split text into top-level pieces: ("text", s) and balanced ("group", "[...]"); stray brackets become text."""
    pieces, start, i = [], 0, 0
    while i < len(text):
        if text[i] == "[":
            close = _close(text, i)
            if close != -1:
                pieces.append(("text", text[start:i]))
                pieces.append(("group", text[i:close + 1]))
                start = i = close + 1
                continue
        i += 1
    pieces.append(("text", text[start:]))
    return [(k, v) for k, v in pieces if v]


def _flatten(op, inner, dropped):
    """Rewrite a span body that holds bracket tokens as a flat run of leaf tokens, at any depth.

    Text takes the nearest enclosing span (or stays plain text when op is None); sounds and cues stay where they
    were written. The result has no nested brackets, so no operator word can end up spoken.
    """
    parts = []
    for kind, piece in _top_level(inner):
        if kind == "text":
            if "[" in piece or "]" in piece:
                dropped.append(Dropped("syntax", "[", "nested"))
                piece = re.sub(r"[\[\]]", "", piece)
            if piece.strip():
                parts.append(f"[{op}: {piece.strip()}]" if op else piece.strip())
            continue
        child = SPAN_OPEN.match(piece)
        if child:
            parts.extend(_flatten(child[1].lower(), piece[child.end():-1], dropped))
        elif "[" in piece[1:-1] or "]" in piece[1:-1]:
            parts.extend(_unwrap(piece[1:-1], op, dropped))
        else:
            parts.append(piece)
    return parts


def _unwrap(body, op, dropped):
    """A cue that wraps other tokens: its own words become one cue, then its inner tokens follow.

    Fragments are joined with a space, so pieces split by a token ("hap[laugh]py") never fuse into a new word.
    """
    pieces = _top_level(body)
    words = " ".join(re.sub(r"[\[\]]", "", v).strip(" ,") for k, v in pieces if k == "text" and v.strip(" ,"))
    words = re.sub(r"\s+", " ", words).strip()
    parts = [f"[{words}]"] if words else []
    return parts + _flatten(op, "".join(v for k, v in pieces if k == "group"), dropped)


def _close(body, i):
    """Index of the bracket that closes the one at i, counting nested pairs; -1 if none."""
    depth = 0
    for j in range(i, len(body)):
        if body[j] == "[":
            depth += 1
        elif body[j] == "]":
            depth -= 1
            if depth == 0:
                return j
    return -1


def cue(raw: str) -> Cue:
    parts = [p.strip() for p in raw.split(",")]
    match = RUNG.match(parts[0])
    rung = "plain"
    if match:
        rung = "slightly" if match[1].lower() in {"slightly", "a little", "somewhat"} else "very"
    words = [RUNG.sub("", p).lower() for p in parts]
    return Cue(raw, parts, rung, words)


def parse(text: str) -> Score:
    header = None
    dropped = []
    body = text
    start = re.match(r"\s*\[\[([^\[\]]*)\]\]", body)
    if start:
        raw = start[1].strip()
        fields = {}
        for part in raw.split(";"):
            key, sep, value = part.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        header = Header(fields or {"register": raw}, raw)
        body = body[start.end():]
        body = re.sub(r"^\s*\.(?:\s+|$)", "", body, count=1)

    sentences = []
    tokens = []

    def append_text(s):
        if not s:
            return
        if tokens and isinstance(tokens[-1], Text):
            tokens[-1] = Text(tokens[-1].s + s)
        else:
            tokens.append(Text(s))

    def plain(s):
        nonlocal tokens
        pos = 0
        for match in BOUNDARY.finditer(s):
            append_text(s[pos:match.start()])
            if tokens:
                sentences.append(Sentence(tokens))
            tokens = []
            pos = match.end()
        append_text(s[pos:])

    i = 0
    while i < len(body):
        if body.startswith("[[", i):
            end = body.find("]]", i + 2)
            if end != -1:
                dropped.append(Dropped("header", "misplaced"))
                i = end + 2
                continue
        if body[i] == "]":
            dropped.append(Dropped("syntax", "]", "stray"))
            i += 1
            continue
        if body[i] != "[":
            end = i + 1
            while end < len(body) and body[end] not in "[]":
                end += 1
            plain(body[i:end])
            i = end
            continue
        opener = SPAN_OPEN.match(body, i)
        if opener:
            op = opener[1].lower()
            close = _close(body, i)
            if close == -1:
                # Never closed: keep the delivery as a sentence cue so "soft:" is not spoken.
                dropped.append(Dropped("span", op, "unterminated"))
                body = body[:i] + f"[{op}] " + body[opener.end():]
                continue
            inner = body[opener.end():close]
            if not inner.strip():
                dropped.append(Dropped("span", op, "empty"))
                body = body[:i] + body[close + 1:]
                continue
            if "[" in inner or "]" in inner:
                # A sound or cue inside a span splits the span around it: [soft: a [laugh] b] → [soft: a] [laugh] [soft: b].
                body = body[:i] + " ".join(_flatten(op, inner, dropped)) + body[close + 1:]
                continue
        end = body.find("]", i + 1)
        nested = body.find("[", i + 1, end if end != -1 else len(body))
        if nested != -1:
            close = _close(body, i)
            if close != -1:
                # [happy [laugh]] → [happy] [laugh]: the cue's words stay a cue instead of being spoken.
                body = body[:i] + " ".join(_unwrap(body[i + 1:close], None, dropped)) + body[close + 1:]
                continue
        if end == -1 or nested != -1:
            # Drop only this bracket and keep scanning, so a later opener is still parsed.
            dropped.append(Dropped("syntax", "[", "unmatched"))
            i += 1
            continue
        content = body[i + 1:end]
        raw = content.strip()
        low = raw.lower()
        span = SPAN.match(raw)
        speaker = re.match(r"^speaker:\s*(\S.*)$", raw, re.I | re.S)
        if low in _events():
            tokens.append(Event(_events()[low], raw))
        elif span:
            op = span[1].lower()
            if BOUNDARY.search(span[2]):
                tokens.append(cue(op))
                plain(" " + span[2])
                dropped.append(Dropped("span", op, "crossed sentence"))
            else:
                tokens.append(Span(op, span[2], raw))
        elif speaker:
            tokens.append(Speaker(speaker[1], raw))
        elif raw:
            tokens.append(cue(content))
        else:
            dropped.append(Dropped("syntax", "[]", "empty"))
        i = end + 1
    if tokens:
        sentences.append(Sentence(tokens))
    return Score(header, sentences, text, dropped)
