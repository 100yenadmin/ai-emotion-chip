"""Value objects shared by the offline core."""
import re
from dataclasses import dataclass, field
from typing import Literal

Rung = Literal["slightly", "plain", "very"]
# The span operators the score grammar parses; dialect tables may only define templates for these.
SPAN_OPS = ("soft", "stress", "slow", "fast", "whisper", "smiling", "emphasis", "forceful")


@dataclass(frozen=True)
class Cue:
    raw: str
    parts: list[str]
    rung: Rung
    words: list[str]


@dataclass(frozen=True)
class Span:
    op: str
    text: str
    raw: str


@dataclass(frozen=True)
class Event:
    id: str
    raw: str


@dataclass(frozen=True)
class Speaker:
    id: str
    raw: str


@dataclass(frozen=True)
class Text:
    s: str


@dataclass(frozen=True)
class Sentence:
    tokens: list[Cue | Span | Event | Speaker | Text]


@dataclass(frozen=True)
class Header:
    fields: dict[str, str]
    raw: str


@dataclass(frozen=True)
class Dropped:
    kind: str
    value: str
    reason: str = ""


@dataclass(frozen=True)
class Score:
    header: Header | None
    sentences: list[Sentence]
    source: str
    dropped: list[Dropped] = field(default_factory=list)


@dataclass(frozen=True)
class Segment:
    index: int
    text: str
    cues: list[Cue]
    spans: list[Span]
    events: list[Event]
    group: str | None
    rung: Rung
    params: dict
    t0: float | None = None
    t1: float | None = None


@dataclass(frozen=True)
class DeliveryPlan:
    segments: list[Segment]


@dataclass(frozen=True)
class Rendered:
    text: str
    params: dict
    dropped: list[Dropped]
    notes: list[str]
    plan: DeliveryPlan
    dialect: str
    dialect_version: str
    group: str | None = None  # the group the call's params came from; streaming pieces pass it as params_from


@dataclass(frozen=True)
class Finding:
    kind: str
    value: str
    reason: str = ""


@dataclass(frozen=True)
class Options:
    sounds_per_turn: int = 1
    params_from: str | dict | None = None
    eleven_stability: float = 0.5
    eleven_similarity: float = 0.75
    allow_speakers: bool = False
    model: str | None = None
    overlay: dict | None = None
    policy_state: dict | None = None  # carried across streamed pieces of one reply; reset per turn


# Stands where a token was removed; render turns it into a space only between two word characters, so dropping
# a token never fuses the words around it ("hap[hysterical]py" stays "hap py").
GAP = "\ue000"


def close_gaps(text):
    return re.sub(r"(?<=\w)\ue000+(?=\w)", " ", text).replace(GAP, "")


def option(options, key, default=None):
    if options is None:
        return default
    if isinstance(options, dict):
        return options.get(key, default)
    return getattr(options, key, default)
