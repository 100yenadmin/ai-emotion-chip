"""Cached YAML loading with explicit, local schema validation."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import yaml

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


def _read(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
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
    _require(isinstance(sounds.get("forbidden_cue_words"), list) and all(isinstance(x, str) for x in sounds["forbidden_cue_words"]), "forbidden_cue_words must be a list of strings")
    for name in DIALECTS:
        dialect = _mapping(data.dialects.get(name), f"dialect {name}")
        _require(isinstance(dialect.get("dialect_version"), str), f"dialect {name}: missing dialect_version")
        phrases = dialect.get("phrases")
        _require(isinstance(phrases, list) and all(isinstance(x, str) for x in phrases), f"dialect {name}: missing phrases")
        inverse = _mapping(dialect.get("inverse"), f"dialect {name} inverse")
        for kind in ("spans", "events"):
            for op, template in _mapping(dialect.get(kind), f"dialect {name} {kind}").items():
                _require(template is None or isinstance(template, str), f"dialect {name}: invalid template")
                for literal in LITERALS.findall(template or ""):
                    _require(literal in phrases, f"dialect {name}: emitted phrase {literal} not in whitelist")
                    _require(literal in inverse, f"dialect {name}: missing inverse for {literal}")
        if dialect.get("speaker"):
            _require(dialect["speaker"] in phrases, f"dialect {name}: speaker missing from whitelist")
    return data


def load_data(path=None):
    return _load(str(Path(path).resolve() if path is not None else Path(__file__).with_name("data").resolve()))


@lru_cache(maxsize=16)
def _load(path):
    root = Path(path)
    groups = _mapping(_read(root / "groups.yaml"), "groups.yaml")
    return validate(Data(root, _read(root / "emotions.yaml"), _read(root / "aliases.yaml"),
                         groups.get("groups", groups), _read(root / "sounds.yaml"),
                         {name: _read(root / "dialects" / f"{name}.yaml") for name in DIALECTS}))


load_data.cache_clear = _load.cache_clear
