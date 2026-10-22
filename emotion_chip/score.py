"""A bracket scanner; vendor angle markup remains ordinary text."""
from bisect import bisect_left
import re
from pathlib import Path
from functools import lru_cache

import yaml

from .model import GAP, SPAN_OPS, Cue, Dropped, Event, Header, Score, Sentence, Span, Speaker, Text

# A period after a common title or "e.g." does not end a sentence ("Dr. Smith arrived." is one sentence).
ABBREVIATIONS = ("Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "Mt", "vs", "e.g", "i.e")
# Up to two closing quotes or parentheses may follow the punctuation ('He said "go." Then', '(He said "go.") Then').
CLOSERS = ("", "[\"'”’)]", "[\"'”’)]{2}")
BOUNDARY = re.compile("".join(rf"(?<!\b{re.escape(a)}\.{c})" for a in ABBREVIATIONS for c in CLOSERS)
                      + "(?:" + "|".join(rf"(?<=[.!?…]{c})" for c in CLOSERS) + r")\s+")
SPAN = re.compile(rf"^({'|'.join(SPAN_OPS)}):\s*(.+)$", re.I | re.S)
SPACE = re.compile(r"\s+")
SPAN_OPEN = re.compile(rf"\[\s*({'|'.join(SPAN_OPS)}):\s*", re.I)
SPEAKER_ID = re.compile(r"\d{1,3}")
INNER = re.compile(r"(\[[^\[\]]*\])")
# A bracket group with no letter names no cue ("items[0]", "[1]", "[::1]"); a Markdown link's target is a URL or path.
LETTER = re.compile(r"[^\W\d_]")
LINK_TARGET = re.compile(r"\(([^\s()]*[/.:#][^\s()]*)\)")
RUNG = re.compile(r"^(slightly|a little|somewhat|very|extremely|deeply|really)\s+", re.I)


@lru_cache(maxsize=1)
def _default_sounds():
    from .data import safe_load_unique
    return safe_load_unique((Path(__file__).with_name("data") / "sounds.yaml").read_text())


def _events(sounds=None):
    """Event ids, plus vendor spellings (laughs, sighing, long pause) mapped to them, from the active data."""
    sounds = sounds if sounds is not None else _default_sounds()
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


@lru_cache(maxsize=16)
def _scan(body):
    """One pass over a body: matched bracket pairs, every ']' and every ']]' start, so lookups stay linear."""
    pairs, stack, closes, doubles = {}, [], [], []
    for j, char in enumerate(body):
        if char == "[":
            stack.append(j)
        elif char == "]":
            closes.append(j)
            if stack:
                pairs[stack.pop()] = j
            if j and body[j - 1] == "]":
                doubles.append(j - 1)
    return pairs, closes, doubles


def _close(body, i):
    """Index of the bracket that closes the one at i, counting nested pairs; -1 if none."""
    return _scan(body)[0].get(i, -1)


def _find_after(positions, start):
    k = bisect_left(positions, start)
    return positions[k] if k < len(positions) else -1


def cue(raw: str) -> Cue:
    parts = [p.strip() for p in raw.split(",")]
    match = RUNG.match(parts[0])
    rung = "plain"
    if match:
        rung = "slightly" if match[1].lower() in {"slightly", "a little", "somewhat"} else "very"
    words = [RUNG.sub("", p).lower() for p in parts]
    return Cue(raw, parts, rung, words)


def header_cue(header):
    """The header's delivery words as one cue, in the order written: register values and bare parts."""
    if header is None:
        return None
    values = [part.partition(":")[2].strip() if ":" in part else part.strip() for part in header.raw.split(";")]
    values = [v for v in values if v]
    return cue(", ".join(values)) if values else None


def parse(text: str, data=None) -> Score:
    events = _events(data.sounds if data is not None else None)
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

    def lead(i):
        """A boundary before a rewrite that starts against a word, so its cue is not read as part of that word."""
        return GAP if i and (body[i - 1].isalnum() or body[i - 1] == "_") else ""

    def ident(i):
        """Whether the character before i ends an identifier. ASCII only: CJK text puts cues against words."""
        return bool(i) and (body[i - 1] < "\x80" and (body[i - 1].isalnum() or body[i - 1] == "_"))

    kept = -1  # where the last bracket group kept as text ends, so a chained subscript ("x[i][j]") stays text too

    def in_code(i):
        """Whether the group at i continues a word or a code expression: x[k], x[i][j], f()[k], items[0][k]."""
        if ident(i) or (i and body[i - 1] == "]" and kept == i - 1):
            return True
        if not i or body[i - 1] != ")":
            return False
        # A call's closing parenthesis: its "(" sits against a word ("factory()"), unlike prose "(He laughed)".
        depth = 0
        for j in range(i - 1, max(-1, i - 202), -1):
            depth += {")": 1, "(": -1}.get(body[j], 0)
            if depth == 0:
                return ident(j)
        return False

    def guard(i):
        """The boundary a rewrite leads with, so its first cue is read as a cue, not as part of a word or code."""
        return GAP if lead(i) or in_code(i) else ""

    i = 0
    while i < len(body):
        if body.startswith("[[", i):
            end = _find_after(_scan(body)[2], i + 2)
            if end != -1:
                inner = body[i + 2:end]
                if any(c in inner for c in ":;[") or not inner.strip():
                    dropped.append(Dropped("header", "misplaced"))
                    append_text(GAP)
                elif in_code(i):
                    # Code such as pandas columns ("df[['a', 'b']]") is text.
                    plain(body[i:end + 2])
                    kept = end + 1
                else:
                    # A wiki link ("[[Project Plan]]") is not header syntax: its words are spoken.
                    plain(inner.strip())
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
                body = body[:i] + guard(i) + f"[{op}] " + body[opener.end():]
                continue
            inner = body[opener.end():close]
            if not inner.strip():
                # Skip it in place: rebuilding the body would rescan it once per empty span.
                dropped.append(Dropped("span", op, "empty"))
                i = close + 1
                continue
            if "[" in inner or "]" in inner:
                # A sound or cue inside a span splits the span around it: [soft: a [laugh] b] → [soft: a] [laugh] [soft: b].
                body = body[:i] + guard(i) + " ".join(_flatten(op, inner, dropped)) + body[close + 1:]
                continue
        end = _find_after(_scan(body)[1], i + 1)
        nested = body.find("[", i + 1, end if end != -1 else len(body))
        if not opener and end != -1 and in_code(i):
            # A group that continues an identifier or a code expression is text: handlers[pause], x[i][j], f()[k],
            # Dict[str, List[int]]. A span ("hap[stress: pi]ness") names its operator, so it stays score syntax.
            close = end if nested == -1 else _close(body, i)
            if close != -1 and not SPAN_OPEN.search(body, i + 1, close):
                plain(body[i:close + 1])
                kept, i = close, close + 1
                continue
        if nested != -1:
            close = _close(body, i)
            if close != -1:
                # [happy [laugh]] → [happy] [laugh]: the cue's words stay a cue instead of being spoken.
                body = body[:i] + guard(i) + " ".join(_unwrap(body[i + 1:close], None, dropped)) + body[close + 1:]
                continue
        if end == -1 or nested != -1:
            # Drop only this bracket and keep scanning, so a later opener is still parsed.
            dropped.append(Dropped("syntax", "[", "unmatched"))
            i += 1
            continue
        content = body[i + 1:end]
        raw = content.strip()
        link = LINK_TARGET.match(body, end + 1)
        if raw and link:
            # Ordinary text, not score syntax: "[docs](https://x.io)" keeps its words and its target.
            plain(f"{raw} ({link[1]})")
            i = link.end()
            continue
        if raw and not LETTER.search(raw):
            append_text(body[i:end + 1])
            i = end + 1
            continue
        low = re.sub(r"\s+", " ", raw).lower()
        span = SPAN.match(raw)
        speaker = re.match(r"^speaker:\s*(.*)$", raw, re.I | re.S)
        if low in events:
            tokens.append(Event(events[low], raw))
        elif span:
            op = span[1].lower()
            if BOUNDARY.search(span[2]):
                tokens.append(cue(op))
                plain(" " + span[2])
                dropped.append(Dropped("span", op, "crossed sentence"))
            else:
                tokens.append(Span(op, span[2], raw))
            # A span that ends its sentence ("[soft: Two.] Three.") holds the punctuation; the space after it is
            # outside, so the boundary is checked here, for a crossed span too.
            gap = SPACE.match(body, end + 1)
            if gap and BOUNDARY.match(span[2] + " ", len(span[2])):
                sentences.append(Sentence(tokens))
                tokens = []
                i = gap.end()
                continue
        elif speaker and SPEAKER_ID.fullmatch(speaker[1].strip()):
            tokens.append(Speaker(speaker[1].strip(), raw))
        elif speaker:
            # Only the documented numeric form: anything else could inject vendor markup into the marker.
            dropped.append(Dropped("speaker", raw, "invalid id"))
            append_text(GAP)
        elif raw:
            tokens.append(cue(content))
        else:
            dropped.append(Dropped("syntax", "[]", "empty"))
        i = end + 1
    if tokens:
        sentences.append(Sentence(tokens))
    return Score(header, sentences, text, dropped)
