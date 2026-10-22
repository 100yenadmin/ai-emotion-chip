"""Cached YAML loading with explicit, local schema validation."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import string

import yaml

from .model import SPAN_OPS
from .score import cue as _cue

DIALECTS = ("fish_s2", "fish_drama3", "eleven_v4", "tagless", "prose")
LITERALS = re.compile(r"\[[^\[\]]+\]|</?\w+>")


@dataclass(frozen=True)
class Data:
    path: Path
    emotions: list[dict]
    aliases: dict
    groups: dict
    sounds: dict
    dialects: dict

    def lookup(self, word):
        for row in self.emotions:
            if row["id"].lower() == word.lower():
                return row
        return self.aliases.get(word.lower())


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that rejects duplicate mapping keys instead of keeping the last one."""


def _unique_mapping(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    duplicates = sorted({str(k) for k in keys if keys.count(k) > 1})
    if duplicates:
        raise yaml.constructor.ConstructorError(None, None, f"duplicate keys: {', '.join(duplicates)}", node.start_mark)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def safe_load_unique(text):
    return yaml.load(text, Loader=_UniqueKeyLoader)


def _read(path):
    try:
        return safe_load_unique(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load {path.name}: {exc}") from exc


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _mapping(value, name):
    _require(isinstance(value, dict), f"{name} must be a mapping")
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cue_like(data, text):
    """True when text, read as a bracket cue, names an emotion: such a literal would voice an unauthored cue."""
    return any(data.lookup(word) for word in _cue(text).words)


def _shadows_score(data, name):
    """True when a sound name would capture a cue or a span/speaker form, since events are matched first."""
    return _cue_like(data, name) or name.partition(":")[0].strip().lower() in SPAN_OPS + ("speaker",)


def validate(data):
    """Validate references and renderer literals as well as required fields."""
    for name, group in _mapping(data.groups, "groups").items():
        _mapping(group, f"group {name}")
        for key in ("tier", "bs_code", "bs_recognition", "fish_speed", "el_stability_offset", "evidence"):
            _require(key in group, f"group {name} missing {key}")
        rec = group["bs_recognition"]
        _require(rec is None or (_number(rec) and 0 <= rec <= 100), f"group {name}: invalid recognition")
        tier = "C" if rec is None or rec < 40 else ("B" if rec < 60 else "A")
        _require(group["tier"] == tier, f"group {name}: tier does not match recognition")
        for key in ("fish_speed", "el_stability_offset"):
            _require(_number(group[key]), f"group {name}: invalid {key}")
        _require(isinstance(group["evidence"], str) and group["evidence"], f"group {name}: missing evidence")
    _require(isinstance(data.emotions, list), "emotions must be a list")
    ids = set()
    for row in data.emotions:
        _mapping(row, "emotion")
        for key in ("id", "paper_cluster", "group", "about", "assistant_ok", "ladder", "post_training_delta", "provenance"):
            _require(key in row, f"emotion missing {key}")
        _require(isinstance(row["id"], str) and row["id"] and row["id"] not in ids, "invalid or duplicate emotion id")
        ids.add(row["id"])
        _require(row["group"] in data.groups, f"unknown group {row['group']}")
        _require(isinstance(row["assistant_ok"], bool), "assistant_ok must be boolean")
        _require("arousal_bucket" not in row or _number(row["arousal_bucket"]), "arousal_bucket must be numeric")
        _require(isinstance(row["ladder"], list) and len(row["ladder"]) == 3 and all(isinstance(x, str) for x in row["ladder"]), "ladder must contain three strings")
        _require(bool(row["provenance"]), "provenance is required")
    for word, row in _mapping(data.aliases, "aliases").items():
        _mapping(row, f"alias {word}")
        _require(row.get("group") in data.groups, f"alias {word}: unknown group")
        _require(_number(row.get("arousal_bucket")), f"alias {word}: invalid arousal_bucket")
    sounds = _mapping(data.sounds, "sounds")
    for name, row in _mapping(sounds.get("events"), "events").items():
        _require(isinstance(row, dict) and row.get("kind") in {"affect", "structural"}, f"event {name}: invalid kind")
        _require(not _shadows_score(data, name), f"event {name} would capture an emotion cue or score form")
    _require(isinstance(sounds.get("forbidden_cue_words"), list) and sounds["forbidden_cue_words"]
             and all(isinstance(x, str) and x.strip() for x in sounds["forbidden_cue_words"]),
             "forbidden_cue_words must be a nonempty list of nonblank strings")
    for alias, target in _mapping(sounds.get("aliases", {}), "sound aliases").items():
        _require(isinstance(alias, str) and alias.strip() and target in sounds["events"],
                 f"sound alias {alias}: target {target} is not an event")
        _require(alias.lower() not in sounds["events"], f"sound alias {alias} would override the event {alias}")
        _require(not _shadows_score(data, alias), f"sound alias {alias} would capture an emotion cue or score form")
    for name in DIALECTS:
        dialect = _mapping(data.dialects.get(name), f"dialect {name}")
        _require(isinstance(dialect.get("dialect_version"), str), f"dialect {name}: missing dialect_version")
        phrases = dialect.get("phrases")
        _require(isinstance(phrases, list) and all(isinstance(x, str) for x in phrases), f"dialect {name}: missing phrases")
        inverse = _mapping(dialect.get("inverse"), f"dialect {name} inverse")
        for kind in ("spans", "events"):
            for op, template in _mapping(dialect.get(kind), f"dialect {name} {kind}").items():
                _require(template is None or isinstance(template, str), f"dialect {name}: invalid template")
                if kind == "spans":
                    _require(op in SPAN_OPS, f"dialect {name}: span {op} is not in the score grammar")
                if kind == "spans" and template not in (None, "uppercase"):
                    try:
                        fields = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(template)
                                  if f is not None]
                    except ValueError:
                        fields = []
                    # One plain {text}: no repeat, no conversion and no format spec, so render() can never raise.
                    _require(fields == [("text", "", None)],
                             f"dialect {name}: span {op} must use exactly one plain {{text}} placeholder")
                if kind == "events":
                    _require(op in sounds["events"], f"dialect {name}: event {op} is not a declared sound event")
                for literal in LITERALS.findall(template or ""):
                    _require(literal in phrases, f"dialect {name}: emitted phrase {literal} not in whitelist")
                    # A renderer literal is syntax; one that names an emotion would voice a cue the agent never wrote.
                    _require(not (literal.startswith("[") and _cue_like(data, literal[1:-1])),
                             f"dialect {name}: emitted phrase {literal} is an emotion cue")
                    _require(literal in inverse, f"dialect {name}: missing inverse for {literal}")
                if kind == "spans" and template not in (None, "uppercase"):
                    # Only declared vendor literals and whitespace may surround {text}: no unmarked spoken words.
                    rest = LITERALS.sub("", template.replace("{text}", ""))
                    _require(not rest.strip(), f"dialect {name}: span {op} adds unmarked text {rest.strip()!r}")
                if kind == "events" and template is not None:
                    # An event is emitted as written: one or more declared literals, nothing spoken.
                    _require(LITERALS.search(template) and not LITERALS.sub("", template).strip(),
                             f"dialect {name}: event {op} must be declared vendor literals only")
        if name == "eleven_v4":
            variants = _mapping(dialect.get("variants"), "dialect eleven_v4 variants")
            _require(all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0
                         for k, v in variants.items()), "dialect eleven_v4: variants must map model ids to positive caps")
        if dialect.get("speaker"):
            _require(dialect["speaker"] in phrases, f"dialect {name}: speaker missing from whitelist")
            try:
                fields = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(dialect["speaker"])
                          if f is not None]
            except ValueError:
                fields = []
            _require(fields == [("id", "", None)], f"dialect {name}: speaker must use exactly one plain {{id}} placeholder")
            # The static part must be vendor marker syntax, which is never spoken ("<|speaker:{id}|>").
            _require(re.fullmatch(r"<\|[^\s<>|{}]*\{id\}[^\s<>|{}]*\|>", dialect["speaker"]) is not None,
                     f"dialect {name}: speaker must be one <|...{{id}}...|> marker")
    return data


def load_data(path=None):
    return _load(str(Path(path).resolve() if path is not None else Path(__file__).with_name("data").resolve()))


@lru_cache(maxsize=16)
def _load(path):
    root = Path(path)
    for model in sorted((root / "models").glob("*.yaml")):
        note = _read(model)
        _require(isinstance(note, dict) and isinstance(note.get("note"), str) and note["note"].strip(),
                 f"model note {model.stem}: must be a mapping with a nonempty string note")
    groups = _mapping(_read(root / "groups.yaml"), "groups.yaml")
    return validate(Data(root, _read(root / "emotions.yaml"), _read(root / "aliases.yaml"),
                         groups.get("groups", groups), _read(root / "sounds.yaml"),
                         {name: _read(root / "dialects" / f"{name}.yaml") for name in DIALECTS}))


load_data.cache_clear = _load.cache_clear
