"""Validated, immutable bench inputs and small shared persistence helpers."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
SETS = ("g1", "g2", "g3", "e3")
MODELS = ("claude-opus-5-5",)
CONDITIONS = ("OFF", "ON", "NATIVE")
RUNS = (1, 2, 3)
FIELDS = {
    "g1": {"id", "framing", "user_message", "false_claim", "correct_fact"},
    "g2": {"id", "risk_cues", "user_message"},
    "g3": {"id", "user_message", "hazard"},
    "e3": {"id", "category", "valence_hint", "user_message"},
}


def sha256(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def validate_item(item, set_name):
    if set_name not in SETS:
        raise ValueError("unknown item set")
    if not isinstance(item, dict) or set(item) != FIELDS[set_name]:
        raise ValueError(f"{set_name}: unexpected or missing item fields")
    for key, value in item.items():
        if key == "risk_cues":
            if type(value) is not bool:
                raise ValueError("risk_cues must be boolean")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a nonempty string")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", item["id"]):
        raise ValueError("invalid item id")
    enums = {"framing": {"neutral", "emotional"}, "category": {"work", "circumstance"},
             "valence_hint": {"positive", "negative", "mixed", "neutral"}}
    for key, choices in enums.items():
        if key in item and item[key] not in choices:
            raise ValueError(f"invalid {key}")
    return item


def load_items(set_name, split="heldout", root=None):
    if set_name not in SETS or split not in {"heldout", "tune"}:
        raise ValueError("unknown set or split")
    directory = (Path(root) if root else BENCH / "items") / set_name / split
    result = []
    for path in sorted(directory.glob("*.yaml")):
        item = validate_item(yaml.safe_load(path.read_text(encoding="utf-8")), set_name)
        if path.stem != item["id"] or any(x["id"] == item["id"] for x in result):
            raise ValueError("duplicate id or filename/id mismatch")
        result.append(item)
    if not result:
        raise ValueError("no items found")
    return result


def heldout_hashes(root=None):
    canonical = root is None
    root = Path(root) if root else BENCH / "items"
    manifest = root / "HELDOUT.sha256"
    if canonical:
        prereg = (ROOT / "EVAL-PREREG.md").read_text(encoding="utf-8")
        pinned = re.search(r"manifest's own sha256 is\s*`([a-f0-9]{64})`", prereg)
        if not pinned or sha256(manifest.read_bytes()) != pinned[1]:
            raise ValueError("heldout manifest does not match Amendment A1")
    expected = dict((name.removeprefix("./"), digest)
                    for digest, name in (line.split() for line in manifest.read_text().splitlines()))
    actual = {p.relative_to(root).as_posix(): sha256(p.read_bytes())
              for p in root.glob("*/heldout/*.yaml")}
    if actual != expected:
        raise ValueError("heldout files do not match HELDOUT.sha256")
    return actual


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".part", delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
        temporary = f.name
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


TMP = Path("/tmp").resolve()


def run_directory(path):
    """A run-id directory for bench output: outside the source tree and outside /tmp."""
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents or path == TMP or TMP in path.parents or path.parent == path:
        raise ValueError("bench output goes in a run-id directory outside the repository and /tmp")
    return path
