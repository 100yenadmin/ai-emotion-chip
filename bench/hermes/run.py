#!/usr/bin/env python3
"""Hermes-native listening runner (v1: Fish, Opus 5.5, bare persona). Paid: Claude subscription turns + Fish TTS/ASR.

    EMOTION_CHIP_BENCH_PATHS=paths.json python -m bench.hermes.run --run-id ID --items smoke|full|p01,p05 \
        [--voice fish|ava] [--warm-only]

paths.json names this machine's host, repos, keys and output dirs; copy paths.example.json and fill it in.

Each item × arm runs one real Hermes agent turn (platform telegram, toolsets tts+skills) in a fresh APFS clone of a
warmed golden home: arm `on` speaks through tts.provider emotion-chip, arm `off` through fish-audio directly. The
host is the fish-audio acceptance cache of Hermes v0.22.2 (read-only), every host command runs under sandbox-exec
with writes only inside the run dir, and plugins are git archives (the DirectSDK provider a read-only copy).

--voice ava swaps the audio path for eva's ElevenLabs voice (eleven_voice_id in paths.json): both arms speak through tts.provider emotion-chip with
backend eleven; arm `on` has guide_mode generate (prompt section and skill), arm `off` guide_mode defer (neither), so
the chip's guidance is the only difference. A credential failure stops the run at that cell and is recorded.

The run dir holds the Fish key (one 0600 env file) and stays in scratch; the Claude token is read from the keychain
per item and passed only through the child's environment. Only key-free evidence is copied to the evidence dir.
v1 has no guardrails: nothing here filters, caps or checks content. The leak check reports, never filters.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import tarfile

from .bench_probe.probe import FIXED_CANARIES

HOME = Path.home()
# The chip plugin is archived from main; each run records the commit it resolved to.
FISH_REF, CHIP_REF = "f7ae982", "main"
PROVIDER = "claude-subscription-directsdk-experimental"
# Where things live on the bench machine comes from a local JSON file this variable names (see paths.example.json),
# so no machine path is kept in the repo.
PATHS_ENV = "EMOTION_CHIP_BENCH_PATHS"
PATH_KEYS = ("host", "host_label", "fish_repo", "chip_repo", "provider_src", "fish_key_file", "runs_root",
             "evidence_root")
HERE = Path(__file__).resolve().parent
MODEL, EFFORT, FISH_MODEL = "claude-opus-5-5", "medium", "s2.1-pro"
# --voice ava: eva's ElevenLabs voice through the emotion-chip provider in BOTH arms; only guide_mode differs.
VOICES = ("fish", "ava")
# The voice id comes from the paths file (eleven_voice_id); main() fills it in for --voice ava.
AVA = {"backend": "eleven", "eleven_voice_id": "", "eleven_model": "eleven_v4", "eleven_style": 0.30, "eleven_speed": 0.90}
# A cell whose turn or voice call failed on a credential stops the run (recorded, never retried).
CREDENTIAL_FAILURE = re.compile(r"rejected your sign-in|access token has been revoked|status 401|HTTP 401|"
                                r"ElevenLabs HTTP 40[13]|invalid_api_key|needs ELEVENLABS_API_KEY|"
                                r"credential unavailable", re.IGNORECASE)
ARMS = ("on", "off")
SUFFIX = "Reply as a voice message."
TOKEN_BAND = 0.05
SANDBOX = """(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath (param "RUN_DIR")))
(allow file-write* (literal "/dev/null") (literal "/dev/tty") (literal "/dev/stdout") (literal "/dev/stderr"))
"""
ITEMS = {
    "p01": "My build finally passed after six hours of failures. I could cry.",
    "p02": "Can you tell me the time difference between Tokyo and London right now, and whether it's a good time to call?",
    "p03": "My dog Biscuit died this morning. She was fourteen.",
    "p04": "I got the job! The one I interviewed for three times!",
    "p05": "You told me the meeting was at 3 and it was at 2. I missed it.",
    "p06": "Read me the last lines of a sad goodbye letter, the kind that makes people cry. Make one up.",
    "p07": "Tell me the funniest thing that could happen at a wedding, as a short story.",
    "p08": "I'm pretty sure the Great Wall of China is visible from the Moon, right?",
    "p09": "Summarise what a database index is in three sentences.",
    "p10": "I can't sleep. It's 3am and my head won't stop.",
    "p11": "Scream at me to get off the couch and go to the gym. Really go for it.",
    "p12": "We shipped the release and nothing broke. First time ever.",
    "p13": "My mum's in hospital and the doctors won't tell me anything yet.",
    "p14": "Whisper me a secret about the stars.",
    "p15": "That's the third time you've got my name wrong. It's Andrea, not Andrew.",
    "p16": "Give me the weather-style forecast for my week: deadlines Monday, dentist Wednesday, beach Saturday.",
    "p17": "Act out a dramatic villain monologue, two lines, full drama.",
    "p18": "I just found out my best friend has been lying to me for years.",
    "p19": "Explain why the sky is blue to a five-year-old.",
    "p20": "We did it. After two years, the house is ours.",
}
SMOKE = ("p01", "p05", "p11")
# Never handed to the child: the provider gets the OAuth token and nothing else of these families.
_FORBIDDEN_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")


def load_paths(env=os.environ):
    """The bench machine's locations, plus extra negative canaries (its own repo paths) for the probe."""
    source = env.get(PATHS_ENV)
    if not source:
        raise SystemExit(f"set {PATHS_ENV} to a JSON file shaped like bench/hermes/paths.example.json")
    raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    missing = [k for k in PATH_KEYS if not raw.get(k)]
    if missing:
        raise SystemExit(f"{source} is missing {missing}")
    paths = {k: raw[k] if k == "host_label" else Path(raw[k]).expanduser() for k in PATH_KEYS}
    # Required: the machine's own repo paths and worktree names must be checked, and they cannot be committed.
    canaries = [str(Path(c).expanduser()) if str(c).startswith("~") else str(c) for c in raw.get("canaries") or []
                if str(c).strip()]
    if not canaries:
        raise SystemExit(f"{source} needs a non-empty 'canaries' list (this machine's repo paths and worktree names)")
    paths["canaries"] = canaries
    # Optional: only --voice ava needs these (the env file holding ELEVENLABS_API_KEY, and the voice to speak in).
    paths["eleven_key_file"] = Path(raw["eleven_key_file"]).expanduser() if raw.get("eleven_key_file") else None
    paths["eleven_voice_id"] = str(raw.get("eleven_voice_id") or "").strip() or None
    return paths


def prompt_for(item_id):
    return ITEMS[item_id] + " " + SUFFIX


def select_items(spec):
    if spec == "smoke":
        return list(SMOKE)
    if spec == "full":
        return list(ITEMS)
    chosen = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in ITEMS]
    if unknown:
        raise SystemExit(f"unknown items: {unknown}")
    if not chosen or len(set(chosen)) != len(chosen):
        raise SystemExit("select at least one item, each once")
    return chosen


def plugin_names(arm, voice="fish"):
    """Plugins in an arm's home. Fish mode loads emotion-chip only in arm on; ava mode loads it in both arms."""
    return ["fish-audio", PROVIDER, "bench-probe"] + (["emotion-chip"] if arm == "on" or voice == "ava" else [])


def hermes_config(arm, claude_dir, voice="fish"):
    """The item home's config.yaml (JSON is valid YAML). Isolation keys are identical in both arms."""
    if arm not in ARMS:
        raise ValueError(arm)
    if voice not in VOICES:
        raise ValueError(voice)
    if voice == "ava" and not AVA["eleven_voice_id"]:
        raise ValueError("--voice ava needs eleven_voice_id in the paths file")
    enabled = plugin_names(arm, voice)
    entries = {PROVIDER: {"settings": {"claude_config_dir": str(claude_dir), "session_mode": True}}}
    if voice == "ava":
        entries["emotion-chip"] = {"settings": dict(AVA, guide_mode="generate" if arm == "on" else "defer")}
    elif arm == "on":
        entries["emotion-chip"] = {"settings": {"guide_mode": "generate"}}
    tts_provider = "emotion-chip" if arm == "on" or voice == "ava" else "fish-audio"
    tts = {"provider": tts_provider, "fish-audio": {"model": FISH_MODEL}}
    if voice == "ava":
        # Plugin builds before the voice-order fix let Hermes' stock tts.elevenlabs.voice_id outrank eleven_voice_id: pin both.
        tts["elevenlabs"] = {"voice_id": AVA["eleven_voice_id"], "model_id": AVA["eleven_model"]}
    return {"model": {"provider": PROVIDER, "default": MODEL},
            "agent": {"reasoning_effort": EFFORT},
            "plugins": {"enabled": enabled, "entries": entries},
            "tts": tts,
            "stt": {"provider": "fish-audio"},
            "auth": {"adopt_external_logins": False},
            "auxiliary": {"title_generation": {"enabled": False}, "background_review": {"enabled": False}},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "curator": {"enabled": False},
            "context": {"engine": "compressor"}}


def child_env(item, fish_key, token=None, probe=True, canaries=(), eleven_key=None):
    """The sandboxed child's whole environment, built from nothing (no inherited ANTHROPIC_*/CLAUDE_CODE_*)."""
    item = Path(item)
    env = {"PATH": f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(item / "user"), "USER": os.environ.get("USER", ""), "LANG": "en_US.UTF-8",
           "HERMES_HOME": str(item / "home"), "HERMES_DISABLE_LAZY_INSTALLS": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "XDG_CACHE_HOME": str(item / "xdg/cache"), "XDG_CONFIG_HOME": str(item / "xdg/config"),
           "XDG_STATE_HOME": str(item / "xdg/state"), "TMPDIR": str(item / "tmp"), "FISH_API_KEY": fish_key}
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if eleven_key:
        env["ELEVENLABS_API_KEY"] = eleven_key
    # Every mode (warm, turn, asr) computes canaries with the same configured set.
    env["BENCH_PROBE_CANARIES"] = json.dumps(list(canaries))
    if probe:
        env["BENCH_PROBE_OUT"] = str(item / "probe.jsonl")
        env["BENCH_PROBE_ITEM"] = str(item)
    bad = [k for k in env if k.startswith(_FORBIDDEN_PREFIXES)
           and k not in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}]
    if bad or "CLAUDECODE" in env:
        raise AssertionError(f"forbidden child env: {bad}")
    return env


def scrub(text, secrets):
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return re.sub(r"(?i)Bearer\s+\S+", "[redacted]", text)


def machine_roots(paths):
    """Every configured location, and this user's home, with the label published in its place.

    A failed turn's diagnostics can name any of them (a traceback through the host's venv or the run dir), so
    published text replaces each, in its written and resolved forms. These are not probe canaries: Hermes' own
    prompt names the cell's home, which lives under runs_root."""
    roots = {}
    for key in (*PATH_KEYS, "eleven_key_file"):
        value = paths.get(key)
        if isinstance(value, Path):
            roots.update({str(value): f"<{key}>", str(value.resolve()): f"<{key}>"})
    roots.update({str(HOME): "~", str(HOME.resolve()): "~"})
    return roots


def label_canaries(text, canaries, roots=None):
    """Published evidence names this machine's canaries (its repo paths) and roots by labels, longest first."""
    labels = {value: f"machine-canary-{i}" for i, value in enumerate(canaries, 1) if value}
    labels.update(roots or {})
    for value in sorted(labels, key=len, reverse=True):
        text = text.replace(value, labels[value])
    return text


def key_value(path, name):
    for line in Path(path).read_text().splitlines():
        if line.startswith(name + "="):
            value = line.partition("=")[2].strip().strip("'\"")
            if value:
                return value
    raise SystemExit(f"{name} missing from its key file")


# Installed environments and front-end packages are not Hermes source.
HOST_SKIP = (".venv", "node_modules", ".git")


def tree_sha256(root, skip=()):
    """sha256 over sorted (relative path, file sha256) pairs, caches, bytecode and `skip` folders excluded."""
    root = Path(root)
    outer = hashlib.sha256()
    skipped = {"__pycache__", *skip}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not skipped & set(p.relative_to(root).parts)
                       and p.suffix != ".pyc"):
        outer.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n")
    return outer.hexdigest()


def words(text):
    text = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text)


def spoken(score):
    """The words a listener should hear: the text minus every bracket token (span bodies kept)."""
    text = re.sub(r"\[\[[^\]]*\]\]", " ", score or "")
    text = re.sub(r"\[(?:soft|stress|slow|fast|whisper|smiling|emphasis|forceful):\s*([^\]]*)\]", r" \1 ", text)
    return re.sub(r"\[[^\]]*\]", " ", text)


_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n):
    """The words ASR writes for a number below 1000 ("45" -> forty five); nothing for larger numbers."""
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (["and"] + number_words(n % 100) if n % 100 else [])
    return []


def leak_words(sent_text, transcript):
    """Words heard that are not in the spoken text (tag words read aloud, mostly). Report only.

    A number in the text counts as its words (ASR writes "3" as "three"), below a thousand; curly apostrophes count
    as straight ones."""
    text = spoken(sent_text)
    expected = set(words(text)) | {w for n in re.findall(r"\d+", text) for w in number_words(int(n))}
    return sorted({w.replace("'", "") for w in words(transcript)} - {w.replace("'", "") for w in expected})


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def first_call_tokens(probe_rows):
    """Upstream prompt tokens (input + cache read + cache write) of API call 1, from post_api_request."""
    for row in probe_rows:
        if row.get("event") == "post_api_request" and row.get("api_call_count") == 1 and row.get("usage"):
            usage = row["usage"]
            if usage.get("prompt_tokens") is not None:
                return usage["prompt_tokens"]
            parts = [usage[k] for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens") if k in usage]
            return sum(parts) if parts else None  # no input field measured: unknown, never zero
    return None


def db_summary(db_path):
    """Usage rows and the session's system prompt (by hash, from system_prompts) from a cell's state.db."""
    out = {"exists": Path(db_path).exists(), "usage_rows": [], "sessions": 0, "system_prompt_chars": None,
           "system_prompt_has_chip_section": None}
    if not out["exists"]:
        return out
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        out["usage_rows"] = [{"model": m, "task": t, "api_call_count": c, "input_tokens": i, "output_tokens": o,
                              "cache_read_tokens": r, "cache_write_tokens": w}
                             for m, t, c, i, o, r, w in db.execute(
                                 "SELECT model, task, api_call_count, input_tokens, output_tokens, cache_read_tokens,"
                                 " cache_write_tokens FROM session_model_usage")]
        out["sessions"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        prompts = db.execute("SELECT p.prompt FROM sessions s JOIN system_prompts p ON p.hash = s.system_prompt_hash"
                             ).fetchall()
    if prompts:
        text = "\n".join(p[0] for p in prompts)
        out["system_prompt_chars"] = len(text)
        out["system_prompt_has_chip_section"] = "Voice delivery (Hermes Emotion Chip)" in text
    return out


def memories_empty(home):
    folder = Path(home) / "memories"
    return not folder.exists() or not any(folder.iterdir())


def cell_asserts(arm, probe_rows, upstream_rows, db, memories_clean, required=FIXED_CANARIES):
    """Asserts 1, 2 and 5 (per cell). 3 and 4 need the whole arm: see arm_asserts.

    Fail closed on missing evidence: an invalid prompt row, an upstream error row, or any row whose canary map lacks a
    required canary (the fixed set plus this machine's configured ones) fails assert 1."""
    pre = [r for r in probe_rows if r.get("event") == "pre_api_request"]
    ups = [r for r in upstream_rows if r.get("event") == "upstream"]
    # The relay must forward to the model the manifest names; a cell answered by another model is not admitted.
    models = sorted({str(r.get("model")) for r in ups})
    errors = [r for r in upstream_rows if r.get("event") != "upstream"]
    invalid = [r for r in pre if r.get("invalid") or not r.get("system_sha256")]
    unchecked = sorted({c for r in pre + upstream_rows for c in required if c not in (r.get("canaries") or {})})
    hits = sorted({c for r in pre + upstream_rows for c, hit in (r.get("canaries") or {}).items() if hit})
    want = arm == "on"
    positive = bool(pre) and not invalid and all(r.get("chip_section") is want and r.get("skill_row") is want
                                                  for r in pre) \
        and bool(ups) and all(r.get("chip_section") is want for r in ups)
    primary = [r for r in db.get("usage_rows", []) if r.get("task") == ""]
    return {
        "1_no_negative_canary": {"pass": bool(pre) and bool(ups) and not errors and not invalid and not unchecked
                                 and not hits and models == [MODEL], "hits": hits, "unchecked_canaries": unchecked,
                                 "upstream_models": models,
                                 "invalid_prompt_rows": len(invalid), "hermes_requests": len(pre),
                                 "upstream_requests": len(ups), "upstream_errors": len(errors)},
        "2_positive_canary": {"pass": positive, "want": want,
                              "chip_section": [r.get("chip_section") for r in pre],
                              "skill_row": [r.get("skill_row") for r in pre],
                              "upstream_chip_section": [r.get("chip_section") for r in ups]},
        "5_db_and_memory": {"pass": len(primary) == 1 and memories_clean, "primary_usage_rows": len(primary),
                            "other_usage_tasks": sorted(r.get("task") for r in db.get("usage_rows", [])
                                                        if r.get("task") != ""),
                            "memories_empty": memories_clean},
    }


def arm_asserts(cells):
    """Asserts 3 and 4 over one arm's cells: {item_id: {"probe": rows, ...}}; adds them to each cell's asserts."""
    hashes = {}
    for item_id, cell in cells.items():
        hashes[item_id] = sorted({r.get("system_sha256") or "<invalid>" for r in cell["probe"]
                                  if r.get("event") == "pre_api_request"})
    reference = Counter(h for hs in hashes.values() if len(hs) == 1 for h in hs if h != "<invalid>").most_common(1)
    reference = reference[0][0] if reference else None
    tokens = {item_id: first_call_tokens(cell["probe"]) for item_id, cell in cells.items()}
    mid = median(v for v in tokens.values() if v is not None and v > 0)
    for item_id, cell in cells.items():
        cell["asserts"]["3_constant_system_hash"] = {"pass": reference is not None and hashes[item_id] == [reference],
                                                     "hashes": hashes[item_id], "arm_reference": reference}
        value = tokens[item_id]
        within = value is not None and value > 0 and bool(mid) and abs(value - mid) <= TOKEN_BAND * mid
        cell["asserts"]["4_call1_tokens_within_band"] = {"pass": within, "call1_prompt_tokens": value,
                                                         "arm_median": mid, "band": TOKEN_BAND}
    return {"reference_hash": reference, "call1_median": mid}


def isolation_pass(cell):
    return all(a["pass"] for a in cell["asserts"].values())


def turn_gate(result):
    """Whether the Hermes turn itself succeeded, kept apart from the isolation asserts. A host error, a nonzero rc,
    an explicit failure or an incomplete turn fails it; a completed turn that simply did not speak passes (unvoiced)."""
    host_error = "rc" in result  # Host.call's own failure shape: no PROOF line or a nonzero exit
    ok = not host_error and result.get("completed") is True and not result.get("failed") and not result.get("error")
    return {"pass": ok, "host_error": host_error, "rc": result.get("rc"), "completed": result.get("completed"),
            "failed": result.get("failed"), "error": (result.get("error") or None) and str(result["error"])[-500:]}


def cell_ok(cell):
    return cell["turn"]["pass"] and isolation_pass(cell)


def blind_pairs(item_ids, cells, seed):
    """For each item voiced in both arms: two clips in random order labelled A/B, and the hidden key."""
    rng = random.Random(seed)
    pairs, key = [], {}
    for item_id in item_ids:
        clips = {arm: cells[arm].get(item_id, {}).get("clip") for arm in ARMS}
        if not all(clips.values()):
            continue
        order = list(ARMS)
        rng.shuffle(order)
        pairs.append({"item": item_id, "prompt": ITEMS[item_id], "A": f"pairs/{item_id}-A.mp3",
                      "B": f"pairs/{item_id}-B.mp3"})
        key[item_id] = {"A": order[0], "B": order[1]}
    return pairs, key, {item: {label: cells[arm][item]["clip"] for label, arm in k.items()} for item, k in key.items()}


def leak_cell(row):
    """Leak words for a transcribed clip; "not checked" when there was a clip but ASR failed; "-" without a clip."""
    if not row.get("clip"):
        return "-"
    if not (row.get("asr") or {}).get("success"):
        return "not checked"
    return ", ".join(row.get("leak_words") or []) or "none"


def _md(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_report(manifest):
    lines = [f"# Hermes-native listening run {manifest['run_id']}", "",
             f"Claim class: `{manifest['claim_class']}`. {manifest['proof_boundary']}", "",
             "## Provenance", "",
             f"- Hermes: {manifest['shas']['hermes']}",
             f"- fish-audio: {manifest['shas']['fish_audio']}",
             f"- emotion-chip: {manifest['shas']['emotion_chip']}",
             f"- DirectSDK provider tree sha256: {manifest['shas']['provider_tree']}",
             f"- bench-probe tree sha256: {manifest['shas']['bench_probe_tree']}",
             f"- Model {manifest['model']} at reasoning effort {manifest['effort']}; "
             f"{manifest.get('voice_label') or 'Fish ' + manifest['fish_model'] + ', default voice'}; "
             "platform telegram; toolsets tts, skills", ""]
    if manifest.get("stopped"):
        lines += [f"**Stopped early:** {manifest['stopped']}", ""]
    lines += ["## Per arm", "",
              "| Arm | Cells | Turns completed | Voiced | Isolation passes | Call-1 prompt tokens (median) |",
              "|---|---|---|---|---|---|"]
    for arm in ARMS:
        a = manifest["arms"].get(arm, {})
        lines.append(f"| {arm} | {a.get('cells', 0)} | {a.get('turn_passes', '-')} | {a.get('voiced', 0)} | "
                     f"{a.get('isolation_passes', 0)} | {a.get('call1_median')} |")
    delta = manifest.get("token_delta", {})
    lines += ["", f"On minus off, call-1 prompt tokens (medians): {delta.get('tokens')}; system prompt chars "
              f"(medians): {delta.get('system_chars')} (about {delta.get('system_chars_as_tokens')} tokens at 4 chars "
              "per token). Report only.", "",
              "## Cells", "", "| Item | Arm | Turn | Isolation | Score the agent wrote | What the voice received | Leak words | "
              "Clip |", "|---|---|---|---|---|---|---|---|"]
    for row in manifest["cells"]:
        failed = [k for k, v in row["asserts"].items() if not v["pass"]]
        turn = row.get("turn") or {}
        turn_text = "-" if not turn else "ok" if turn.get("pass") else "FAILED"
        lines.append(f"| {row['item']} | {row['arm']} | {turn_text} | "
                     f"{'pass' if not failed else 'FAIL ' + ', '.join(failed)} | "
                     f"{_md(' / '.join(c.get('text') or '' for c in row['tts_calls'])) or '(no text_to_speech call)'} | "
                     f"{_md(' / '.join(s.get('text') or '' for s in row['sent_to_voice'])) or '-'} | "
                     f"{leak_cell(row)} | {row.get('clip') or '-'} |")
    lines += ["", "## Final replies", ""]
    for row in manifest["cells"]:
        lines.append(f"- {row['item']} {row['arm']}: {_md(row['final_response'])[:400]}")
    lines += ["", "## What this does not prove", "", manifest["does_not_prove"], ""]
    return "\n".join(lines)


class Host:
    """Sandboxed calls into the acceptance host for one item dir."""

    def __init__(self, run, fish_key, paths, eleven_key=None):
        self.run, self.fish_key, self.paths, self.eleven_key = Path(run), fish_key, paths, eleven_key

    def call(self, item, args_obj, token=None, timeout=600):
        item = Path(item)
        args_path = item / f"args-{args_obj['mode']}.json"
        args_path.write_text(json.dumps(args_obj), encoding="utf-8")
        cmd = ["sandbox-exec", "-D", f"RUN_DIR={self.run}", "-f", str(self.run / "sandbox.sb"),
               str(self.paths["host"] / ".venv/bin/python"), str(self.run / "tools/turn.py"), str(args_path)]
        env = child_env(item, self.fish_key, token, probe=args_obj["mode"] == "turn", canaries=self.paths["canaries"],
                        eleven_key=self.eleven_key)
        if args_obj.get("text"):
            env["CLAUDE_SUBSCRIPTION_DIRECTSDK_SESSION_MODE"] = "0"
            if args_obj.get("claude_command"):
                env["CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND"] = args_obj["claude_command"]
        try:
            p = subprocess.run(cmd, cwd=self.run / "cwd", env=env, capture_output=True, text=True, timeout=timeout)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out = "timeout", (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        out = scrub(out, [self.fish_key, self.eleven_key, token])
        (item / f"log-{args_obj['mode']}.txt").write_text(out, encoding="utf-8")
        found = [line.split("PROOF=", 1)[1] for line in out.splitlines() if line.startswith("PROOF=")]
        if rc or not found:
            return {"error": out[-3000:], "rc": rc}
        return json.loads(found[-1])


def archive(repo, ref, dest):
    dest.mkdir(parents=True)
    with subprocess.Popen(["git", "-C", str(repo), "archive", ref], stdout=subprocess.PIPE) as p:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            tf.extractall(dest, filter="data")
        if p.wait():
            raise SystemExit(f"git archive {repo} {ref} failed")


def rev(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref + "^{commit}"], capture_output=True, text=True,
                          check=True).stdout.strip()


def make_dirs(item):
    for sub in ("home", "user", "tmp", "xdg/cache", "xdg/config", "xdg/state", "out"):
        (item / sub).mkdir(parents=True, exist_ok=True)


def prepare(run, fish_key, paths, eleven_key=None):
    """Run dir skeleton, plugin sources, tools and one warmed golden home per arm."""
    for sub in ("cwd", "tools", "src", "golden"):
        (run / sub).mkdir(parents=True)
    (run / "sandbox.sb").write_text(SANDBOX)
    fd = os.open(run / "secrets.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"FISH_API_KEY={fish_key}\n")
        if eleven_key:
            f.write(f"ELEVENLABS_API_KEY={eleven_key}\n")
    shutil.copyfile(HERE / "turn.py", run / "tools/turn.py")
    shutil.copyfile(HERE / "bench_probe/probe.py", run / "tools/probe.py")
    # Resolve each ref once and archive that commit, so a branch that moves mid-prepare cannot split sha and tree.
    fish_sha, chip_sha = rev(paths["fish_repo"], FISH_REF), rev(paths["chip_repo"], CHIP_REF)
    archive(paths["fish_repo"], fish_sha, run / "src/fish-audio")
    archive(paths["chip_repo"], chip_sha, run / "src/emotion-chip")
    shutil.copytree(paths["provider_src"], run / "src" / PROVIDER, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(HERE / "bench_probe", run / "src/bench-probe", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The label is the operator's description; the tree digest pins the Hermes source files the cells ran.
    shas = {"hermes": paths["host_label"], "hermes_tree": tree_sha256(paths["host"], skip=HOST_SKIP),
            "fish_audio": f"v1.8.0 {fish_sha}", "emotion_chip": f"{CHIP_REF} {chip_sha}",
            "provider_tree": tree_sha256(run / "src" / PROVIDER), "provider_source_tree": tree_sha256(paths["provider_src"]),
            "bench_probe_tree": tree_sha256(run / "src/bench-probe")}
    if shas["provider_tree"] != shas["provider_source_tree"]:
        raise SystemExit("DirectSDK copy differs from its source")
    return shas


def build_home(item, arm, run, voice="fish"):
    home = item / "home"
    plugins = home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    for name in plugin_names(arm, voice):
        subprocess.run(["cp", "-c", "-R", str(run / "src" / name), str(plugins / name)], check=True)
    (home / "config.yaml").write_text(json.dumps(hermes_config(arm, item / "claude", voice), indent=2))


def warm_golden(host, run, arm, token, voice="fish"):
    golden = run / "golden" / arm
    make_dirs(golden)
    (golden / "claude").mkdir()
    build_home(golden, arm, run, voice)
    result = host.call(golden, {"mode": "warm", "item_dir": str(golden), "cwd": str(run / "cwd")}, token=token)
    if "error" in result:
        raise SystemExit(f"STOP: Hermes or a plugin failed to load while warming the {arm} golden home "
                         f"(log {golden}/log-warm.txt):\n{result['error'][-1500:]}")
    db = db_summary(golden / "home/state.db")
    if db["sessions"] or db["usage_rows"]:
        raise SystemExit(f"STOP: warming the {arm} golden home created sessions or usage rows")
    return result


def clone_item(run, arm, item_id, voice="fish"):
    item = run / "items" / arm / item_id
    item.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-R", str(run / "golden" / arm), str(item)], check=True)
    for stale in ("args-warm.json", "log-warm.txt"):
        if (item / stale).exists():
            (item / stale).unlink()
    claude = item / "claude"
    # Fresh config: the golden's claude_config_dir points at the golden; nothing of the golden's claude dir is reused.
    (item / "home/config.yaml").write_text(json.dumps(hermes_config(arm, claude, voice), indent=2))
    return item


def concat_clips(clips, dest):
    if len(clips) == 1:
        shutil.copyfile(clips[0], dest)
        return True
    listing = dest.with_suffix(".txt")
    listing.write_text("".join(f"file '{c}'\n" for c in clips))
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                        str(dest)], capture_output=True, text=True)
    listing.unlink()
    return p.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def credential_failure(cell):
    """The credential error a cell hit (its turn, its final reply or a voice call), or None."""
    texts = [cell.get("error") or "", cell.get("final_response") or ""]
    texts += [str(r.get("error") or "") for r in cell.get("tts_results") or []]
    for text in texts:
        match = CREDENTIAL_FAILURE.search(text)
        if match:
            return match.group(0)
    return None


def run_cell(host, run, arm, item_id, token_loader, voice="fish"):
    item = clone_item(run, arm, item_id, voice)
    token = token_loader()
    result = host.call(item, {"mode": "turn", "item_dir": str(item), "cwd": str(run / "cwd"), "voice": voice,
                              "prompt": prompt_for(item_id)}, token=token, timeout=900)
    del token
    probe_rows, upstream_rows = read_jsonl(item / "probe.jsonl"), read_jsonl(item / "upstream.jsonl")
    db = db_summary(item / "home/state.db")
    cell = {"item": item_id, "arm": arm, "prompt": prompt_for(item_id), "item_dir": str(item),
            "error": result.get("error"), "rc": result.get("rc"),
            "final_response": result.get("final_response", ""), "tts_calls": result.get("tts_calls", []),
            "tts_results": result.get("tts_results", []), "sent_to_voice": result.get("sent_to_voice", []),
            "last_render": result.get("last_render", []), "route": result.get("route"),
            "relays": result.get("relays"), "api_calls": result.get("api_calls"), "db": db,
            "probe": probe_rows, "upstream": upstream_rows,
            "turn": turn_gate(result),
            "asserts": cell_asserts(arm, probe_rows, upstream_rows, db, memories_empty(item / "home"),
                                    FIXED_CANARIES + tuple(c for c in host.paths["canaries"]
                                                           if c not in FIXED_CANARIES))}
    clips = [item / "out" / s["clip"] for s in cell["sent_to_voice"] if s.get("clip")]
    cell["voiced"] = bool(cell["tts_calls"]) and bool(clips)
    cell["clip"] = None
    if clips:
        dest = run / "clips" / f"{item_id}-{arm}.mp3"
        dest.parent.mkdir(exist_ok=True)
        if concat_clips(clips, dest):
            cell["clip"] = f"clips/{dest.name}"
    delivery = item / "home/plugin-data/emotion-chip/delivery.jsonl"
    cell["delivery"] = read_jsonl(delivery)
    cell["credential_failure"] = credential_failure(cell)
    cell["voice_mismatch"] = voice_mismatch(cell, voice)
    return cell


def voice_mismatch(cell, voice):
    """In ava mode, any voice call that did not go to Ava on eleven_v4 (a wrong voice is not the bench)."""
    if voice != "ava":
        return None
    wrong = sorted({f"{s.get('voice')}/{s.get('model')}" for s in cell.get("sent_to_voice") or []
                    if s.get("voice") != AVA["eleven_voice_id"] or s.get("model") != AVA["eleven_model"]})
    return ", ".join(wrong) or None


def stop_reason(cell):
    """Why the run must stop after this cell (paid work ends at the first bad cell), or None."""
    if cell.get("voice_mismatch"):
        return f"wrong voice at {cell['item']} {cell['arm']}: {cell['voice_mismatch']}"
    if cell.get("credential_failure"):
        return f"credential failure at {cell['item']} {cell['arm']}: {cell['credential_failure']}"
    if not cell["turn"]["pass"]:
        return f"turn failed at {cell['item']} {cell['arm']}"
    failed = [k for k, v in cell["asserts"].items() if not v["pass"]]
    if failed:
        return f"isolation asserts failed at {cell['item']} {cell['arm']}: {', '.join(failed)}"
    return None


def run_cells(item_ids, run_one):
    """Run item x arm cells in order, stopping at the first cell with a stop reason; returns (cells, stopped)."""
    cells = {arm: {} for arm in ARMS}
    for item_id in item_ids:
        for arm in ARMS:
            try:
                cell = run_one(arm, item_id)
            except RuntimeError as exc:  # keychain_token: the Claude credential is gone
                stopped = f"credential failure before {item_id} {arm}: {exc}"
                print(f"STOP: {stopped}; no later cell was run", flush=True)
                return cells, stopped
            except Exception as exc:  # a clone or probe file broke: stop, but keep the cells already run
                stopped = f"cell error at {item_id} {arm}: {type(exc).__name__}"
                print(f"STOP: {stopped}; no later cell was run", flush=True)
                return cells, stopped
            cells[arm][item_id] = cell
            print(f"{item_id} {arm}: voiced={cell['voiced']} turn={'ok' if cell['turn']['pass'] else 'FAILED'} "
                  f"credential={cell.get('credential_failure') or 'ok'} "
                  f"asserts={ {k: v['pass'] for k, v in cell['asserts'].items()} }", flush=True)
            stopped = stop_reason(cell)
            if stopped:
                print(f"STOP: {stopped}; no later cell was run", flush=True)
                return cells, stopped
    return cells, None


def leak_check(host, run, cells):
    for arm in ARMS:
        voiced = {item_id: c for item_id, c in cells[arm].items() if c.get("clip")}
        if not voiced:
            continue
        # A fresh clone of the arm's golden: its home carries stt.provider fish-audio and the fish plugin.
        asr_dir = run / "asr" / arm
        asr_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-c", "-R", str(run / "golden" / arm), str(asr_dir)], check=True)
        out = host.call(asr_dir, {"mode": "asr", "item_dir": str(asr_dir),
                                  "clips": [str(run / c["clip"]) for c in voiced.values()]}, timeout=900)
        for item_id, c in voiced.items():
            row = out.get(Path(c["clip"]).name) if "error" not in out else None
            c["asr"] = row or {"success": False, "error": (out.get("error") or "no ASR row")[-300:]}
            sent = " ".join(s.get("text") or "" for s in c["sent_to_voice"])
            c["leak_words"] = leak_words(sent, c["asr"].get("transcript")) if c["asr"].get("success") else []
    for arm in ARMS:
        for c in cells[arm].values():
            c.setdefault("leak_words", [])
            c.setdefault("asr", None)


def voice_label(voice):
    if voice == "ava":
        return (f"ElevenLabs {AVA['eleven_model']} voice {AVA['eleven_voice_id']} (Ava, eva's voice), style "
                f"{AVA['eleven_style']}, speed {AVA['eleven_speed']}, through the emotion-chip provider in both arms "
                "(guide_mode generate on, defer off)")
    return f"Fish {FISH_MODEL}, default voice"


def summarise(run_id, shas, item_ids, cells, arm_refs, voice="fish", stopped=None):
    arms = {}
    for arm in ARMS:
        cs = list(cells[arm].values())
        arms[arm] = {"cells": len(cs), "voiced": sum(1 for c in cs if c.get("clip")),
                     "isolation_passes": sum(1 for c in cs if isolation_pass(c)),
                     "turn_passes": sum(1 for c in cs if c["turn"]["pass"]),
                     "leak_cells": sum(1 for c in cs if c.get("leak_words")),
                     "call1_median": arm_refs[arm]["call1_median"], "reference_hash": arm_refs[arm]["reference_hash"],
                     "system_chars_median": median([next((r["system_chars"] for r in c["probe"]
                                                          if r.get("event") == "pre_api_request"), None) for c in cs])}
    on, off = arms["on"], arms["off"]
    delta_tokens = (on["call1_median"] - off["call1_median"]) if on["call1_median"] is not None and \
        off["call1_median"] is not None else None
    delta_chars = (on["system_chars_median"] - off["system_chars_median"]) if on["system_chars_median"] is not None \
        and off["system_chars_median"] is not None else None
    synth = "ElevenLabs" if voice == "ava" else "Fish"
    unproven = ("Fish, other ElevenLabs voices, models or settings" if voice == "ava"
                else "ElevenLabs, other Fish voices or models")
    return {"run_id": run_id, "claim_class": "local_smoke", "voice": voice, "voice_label": voice_label(voice),
            "stopped": stopped,
            "proof_boundary": f"Real Hermes agent turns through an isolated v0.22.2 host with real {synth} synthesis. "
                              "This dir is unblinded (clips named by arm, the A/B key beside the pairs); listening "
                              "happens on a blind page built from it.",
            "does_not_prove": f"Listener preference, {unproven}, eva's real profile (no eva files, persona or "
                              "LCM-X), GPT models, or any behaviour beyond these prompts on one run each. The leak "
                              "check is one Fish ASR pass and reports only. It is not an E7 session: no catch "
                              "trials; the arms are separate end-to-end turns (guidance on vs off, different text "
                              "per arm), so neither E7 comparison a nor b; the chip comes from an unfrozen ref "
                              f"({CHIP_REF}, resolved commit recorded)"
                              + ("; ElevenLabs seeds are not locked." if voice == "ava" else "."),
            "shas": shas, "model": MODEL, "effort": EFFORT, "fish_model": FISH_MODEL, "suffix": SUFFIX,
            "items": item_ids, "arms": arms,
            "token_delta": {"tokens": delta_tokens, "system_chars": delta_chars,
                            "system_chars_as_tokens": round(delta_chars / 4) if delta_chars is not None else None},
            "cells": [cells[arm][i] for i in item_ids for arm in ARMS if i in cells[arm]]}


def evidence_cell(cell):
    """The key-free, path-free view of a cell for the evidence dir."""
    keep = ("item", "arm", "prompt", "error", "rc", "final_response", "tts_calls", "tts_results", "sent_to_voice",
            "last_render", "route", "relays", "api_calls", "db", "asserts", "voiced", "clip", "asr", "leak_words",
            "delivery", "credential_failure", "voice_mismatch", "turn")
    return {k: cell.get(k) for k in keep}


def publish(run, evidence, manifest, pairs, key, pair_clips, secrets, canaries=(), roots=None):
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / "clips").mkdir()
    (evidence / "pairs").mkdir()
    (evidence / "probe").mkdir()
    public = dict(manifest, cells=[evidence_cell(c) for c in manifest["cells"]])
    texts = {"manifest.json": json.dumps(public, indent=2, default=str), "report.md": render_report(public),
             "pairs.json": json.dumps({"run_id": manifest["run_id"], "pairs": pairs}, indent=2),
             "pairs-key.json": json.dumps({"run_id": manifest["run_id"], "key": key}, indent=2)}
    for name, text in texts.items():
        clean = scrub(text, secrets)
        (evidence / name).write_text(label_canaries(clean, canaries, roots), encoding="utf-8")
        (run / name).write_text(clean, encoding="utf-8")
    for c in manifest["cells"]:
        stem = f"{c['item']}-{c['arm']}"
        for kind in ("probe", "upstream"):
            rows = c[kind]
            (evidence / "probe" / f"{stem}.{kind}.jsonl").write_text(label_canaries(
                scrub("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), secrets), canaries, roots),
                encoding="utf-8")
        if c.get("delivery"):
            (evidence / "probe" / f"{stem}.delivery.jsonl").write_text(label_canaries(
                scrub("".join(json.dumps(r, sort_keys=True) + "\n" for r in c["delivery"]), secrets), canaries, roots),
                encoding="utf-8")
        if c.get("clip"):
            shutil.copyfile(run / c["clip"], evidence / c["clip"])
    for item_id, labels in pair_clips.items():
        for label, clip in labels.items():
            shutil.copyfile(run / clip, evidence / "pairs" / f"{item_id}-{label}.mp3")
    leaked = [n for n in evidence.rglob("*") if n.is_file() and n.suffix in (".json", ".md", ".jsonl")
              and any(s and s in n.read_text(errors="replace") for s in [*secrets, *canaries, *(roots or {})])]
    if leaked:
        raise SystemExit(f"STOP: a secret or machine path reached the evidence dir: {leaked}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--items", default="smoke")
    ap.add_argument("--warm-only", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--voice", choices=VOICES, default="fish")
    args = ap.parse_args(argv)
    # The run id names one folder under each configured root; a path here could put the key file anywhere.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_id):
        raise SystemExit("--run-id must be a plain name: letters, digits, '.', '_' or '-'")
    from bench.gen import keychain_token
    paths = load_paths()
    run, evidence = paths["runs_root"] / args.run_id, paths["evidence_root"] / args.run_id
    if run.exists() or evidence.exists():
        raise SystemExit("run dir or evidence dir already exists; pick a new run id")
    item_ids = select_items(args.items)
    try:
        fish_key = key_value(paths["fish_key_file"], "FISH_API_KEY")
        eleven_key = None
        if args.voice == "ava":
            if paths["eleven_key_file"] is None or paths["eleven_voice_id"] is None:
                raise RuntimeError("eleven_key_file and eleven_voice_id must be set in the paths file")
            eleven_key = key_value(paths["eleven_key_file"], "ELEVENLABS_API_KEY")
            AVA["eleven_voice_id"] = paths["eleven_voice_id"]
        probe_token = keychain_token()
    except (OSError, RuntimeError, SystemExit) as exc:
        raise SystemExit(f"STOP: credential missing: {type(exc).__name__}: {exc}")
    run.mkdir(parents=True)
    shas = prepare(run, fish_key, paths, eleven_key)
    host = Host(run, fish_key, paths, eleven_key)
    warmed = {arm: warm_golden(host, run, arm, probe_token, args.voice) for arm in ARMS}
    (run / "warm.json").write_text(json.dumps(warmed, indent=2))
    print(json.dumps({arm: {k: v for k, v in w.items() if k != "tools"} for arm, w in warmed.items()}, indent=2))
    if args.warm_only:
        return 0
    del probe_token
    cells, stopped = run_cells(item_ids, lambda arm, item_id: run_cell(host, run, arm, item_id, keychain_token,
                                                                        args.voice))
    done = [i for i in item_ids if any(i in cells[arm] for arm in ARMS)]
    arm_refs = {arm: arm_asserts(cells[arm]) for arm in ARMS}
    if not stopped:
        arm_failed = [f"{c['item']} {c['arm']}" for arm in ARMS for c in cells[arm].values() if not isolation_pass(c)]
        if arm_failed:
            stopped = f"arm-level isolation asserts failed at {', '.join(arm_failed)}; ASR skipped"
            print(f"STOP: {stopped}", flush=True)
    if not stopped:
        leak_check(host, run, cells)
    manifest = summarise(args.run_id, shas, done, cells, arm_refs, args.voice, stopped)
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    pairs, key, pair_clips = blind_pairs(done, cells, seed)
    try:
        secrets = [fish_key, eleven_key, keychain_token()]
    except RuntimeError:
        secrets = [fish_key, eleven_key]
    publish(run, evidence, manifest, pairs, key, pair_clips, secrets, canaries=host.paths["canaries"],
            roots=machine_roots(paths))
    failed = [(c["item"], c["arm"], [k for k, v in c["asserts"].items() if not v["pass"]])
              for c in manifest["cells"] if not isolation_pass(c)]
    turn_failed = [(c["item"], c["arm"]) for c in manifest["cells"] if not c["turn"]["pass"]]
    print(json.dumps({"run": str(run), "evidence": str(evidence), "arms": manifest["arms"],
                      "token_delta": manifest["token_delta"], "isolation_failures": failed,
                      "turn_failures": turn_failed, "stopped": stopped}, indent=2))
    return 2 if stopped else 1 if failed or turn_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
