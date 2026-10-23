"""Frozen E7 generation and blind bundling. No credentials are loaded by this parent process.

Only the sandboxed child modes below import Hermes or synthesize speech. Dry runs use a
quiet 1 kHz tone and deterministic fixture replies; they cannot establish listening quality.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from bench.hermes import run

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SEED_ROOT = "E7-eleven-v2"
PROMPTS_SHA256 = "8d8498b85d1e3525e9019e3a5bbed0ec2945d451b0b630cd21d2d034702b534c"
VOICE_SETTINGS = {"speed": 0.90, "stability": 0.5, "similarity_boost": 0.75, "style": 0.30}
INPUTS = {"eva_soul": "SOUL.md", "eva_voice": "voice.md", "eva_skill_dir": "speech-authorship",
          "eva_sag_wrapper": "sag-original.sh"}
FIXED_MTIME = 946684800


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value, *, exclusive=False):
    path = Path(path)
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def prompts():
    if sha(HERE / "prompts.json") != PROMPTS_SHA256:
        raise ValueError("prompts changed")
    return read(HERE / "prompts.json")


def seed_for(item):
    return random.Random(f"{SEED_ROOT}:{item}").randrange(2**32)


def frozen_tag(tag, chip_repo, bench_repo=ROOT):
    """Use only the tag namespace, including annotated tags, and require two clean checkouts."""
    if not tag or tag.startswith("refs/") or any(c in tag for c in " ~^:?*[\\"):
        raise ValueError("expected a tag name")
    try:
        commit = run.rev(chip_repo, "refs/tags/" + tag)
    except subprocess.CalledProcessError:
        raise ValueError("tag does not exist (branches are refused)") from None
    for repo in (chip_repo, bench_repo):
        if run.rev(repo, "HEAD") != commit:
            raise ValueError("bench HEAD must equal frozen tag commit")
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, check=True).stdout
        if status:
            raise ValueError("tracked checkout is dirty")
    return commit


def load_paths():
    source = os.environ.get(run.PATHS_ENV)
    if not source:
        raise ValueError("set EMOTION_CHIP_BENCH_PATHS to the paths JSON")
    raw = read(Path(source).expanduser())
    needed = (*run.PATH_KEYS, *INPUTS, "sag_binary", "ava_voice_id", "eleven_key_file", "canaries")
    if any(not raw.get(k) for k in needed):
        raise ValueError("incomplete E7 paths file")
    literals = {"host_label", "ava_voice_id", "canaries"}
    return {k: v if k in literals else Path(v).expanduser() for k, v in raw.items() if k in needed}


def input_hashes(folder):
    return {name: run.tree_sha256(folder / name) if (folder / name).is_dir() else sha(folder / name)
            for name in INPUTS.values()}


def snapshot(session, paths, expected=None):
    """Freeze private inputs once, comparing future callers without ever publishing contents."""
    dest = session / "inputs"
    incoming = {name: run.tree_sha256(paths[key]) if Path(paths[key]).is_dir() else sha(paths[key])
                for key, name in INPUTS.items()}
    if dest.exists():
        actual = input_hashes(dest)
        if actual != incoming or (expected is not None and actual != expected):
            raise ValueError("different or modified eva snapshot")
        return actual
    dest.mkdir(mode=0o700)
    for key, name in INPUTS.items():
        source = Path(paths[key])
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(source, dest / name)
    actual = input_hashes(dest)
    if actual != incoming:
        raise ValueError("eva inputs changed during snapshot")
    return actual


def seeded_wrapper(source, binary, voice):
    """Keep the source wrapper verbatim except its arity check and the fourth seed argument."""
    if source.count('"$#" -ne 3') != 1 or source.count('--no-play') != 1:
        raise ValueError("cannot reproduce sag wrapper")
    tokens = shlex.split(source[source.index("exec "):].replace("\\\n", " "))
    expected = ["exec", str(binary), "speak", "-f", "$input_path", "-o", "$output_path", "-v", voice,
                "--model-id", "eleven_v4", "--format", "$sag_format", "--speed", "0.90", "--stability", "0.5",
                "--similarity", "0.75", "--style", "0.30", "--normalize", "off", "--lang", "en", "--no-play"]
    if tokens != expected:
        raise ValueError("sag flags, binary or voice differ from paths file")
    return source.replace('"$#" -ne 3', '"$#" -ne 4').replace('--no-play', '--seed "$4" \\\n  --no-play')


def config(item, arm, session, paths, seed):
    cfg = run.hermes_config("on", item / "claude", "fish")
    enabled = [run.PROVIDER, "bench-probe"] + (["emotion-chip"] if arm == "chip" else [])
    cfg["plugins"]["enabled"] = enabled
    cfg["agent"]["reasoning_effort"] = "high"
    cfg.update(context_file_max_chars=100000, context_file_read_timeout=600)
    cfg["stt"] = {"provider": "none"}
    entries = cfg["plugins"]["entries"]
    entries.pop("emotion-chip", None)
    voice = paths["ava_voice_id"]
    if arm == "chip":
        entries["emotion-chip"] = {"settings": dict(run.AVA, eleven_voice_id=voice, eleven_seed=seed,
                                                    guide_mode="generate")}
        cfg["tts"] = {"provider": "emotion-chip", "voice": voice,
                      "elevenlabs": {"voice_id": voice, "model_id": "eleven_v4"}}
    else:
        wrapper = item / "sag-seeded.sh"
        cfg["tts"] = {"provider": "e7-sag", "voice": voice, "providers": {"e7-sag": {
            "type": "command", "output_format": "mp3", "voice": voice, "model": "eleven_v4",
            "env_passthrough": ["ELEVENLABS_API_KEY"],
            "command": f"sh {shlex.quote(str(wrapper))} {{input_path}} {{output_path}} {{format}} {seed}"}}}
    return cfg


def build_home(item, arm, session, paths, seed):
    run.make_dirs(item)
    (item / "claude").mkdir()
    cfg = config(item, arm, session, paths, seed)
    home = item / "home"
    (home / "plugins").mkdir()
    for name in cfg["plugins"]["enabled"]:
        shutil.copytree(session / "src" / name, home / "plugins" / name)
    soul = (session / "inputs/SOUL.md").read_bytes()
    if arm == "eva":
        soul += b"\n\n" + (session / "inputs/voice.md").read_bytes()
        shutil.copytree(session / "inputs/speech-authorship", home / "skills/speech-authorship")
        wrapper = seeded_wrapper((session / "inputs/sag-original.sh").read_text(), paths["sag_binary"],
                                 paths["ava_voice_id"])
        (item / "sag-seeded.sh").write_text(wrapper)
    (home / "SOUL.md").write_bytes(soul)
    write(home / "config.yaml", cfg)
    return sha(home / "SOUL.md")


class E7Host(run.Host):
    """Reuse Host.call's sandbox and empty child_env; provision credentials only inside its child."""
    def call(self, item, args_obj, token=None, timeout=900):
        args_obj = dict(args_obj, chip_root=str(self.run / "src/emotion-chip"),
                        inputs=str(self.run / "inputs"), key_file=str(self.paths["eleven_key_file"]))
        return super().call(item, args_obj, token=token, timeout=timeout)


def rewrite_output(item, output, rewrites):
    item = Path(item).resolve()
    candidate = Path(output).expanduser() if output else item / "out/tts.mp3"
    candidate = (item / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not candidate.is_relative_to(item):
        replacement = item / "out" / f"redirect-{len(rewrites) + 1}.mp3"
        rewrites.append({"original": str(output), "rewritten": str(replacement)})
        candidate = replacement
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return str(candidate)


def install_capture(args, driver):
    """Capture after prepare_spoken_text, before conversion, on both supported provider paths."""
    from tools import tts_tool
    sent, scores, rewrites = [], [], []
    original_tool = tts_tool.text_to_speech_tool

    def tool(text, output_path=None, **kwargs):
        return original_tool(text, output_path=rewrite_output(args["item_dir"], output_path, rewrites), **kwargs)
    tts_tool.text_to_speech_tool = tool
    if args["arm"] == "eva":
        tts_tool._generate_command_tts = driver._recorder(args["item_dir"], sent,
            tts_tool._generate_command_tts, "sag", lambda k: {"route": "sag"})
    else:
        from agent.tts_registry import get_provider
        chip = get_provider("emotion-chip")
        original_prepare = chip._prepare

        def prepare(text, *a, **k):
            scores.append(text)
            return original_prepare(text, *a, **k)
        chip._prepare = prepare
        # Fix E7 voice settings in both comparisons; syntax is the only paired render variable.
        original_synth = chip.eleven.synthesize

        def synth(text, output_path, **kwargs):
            kwargs["params"] = dict(VOICE_SETTINGS)
            return original_synth(text, output_path, **kwargs)
        chip.eleven.synthesize = driver._recorder(args["item_dir"], sent, synth, "eleven", lambda k: {
            "voice": k.get("voice"), "model": k.get("model"), "params": dict(VOICE_SETTINGS),
            "seed": k.get("seed"), "previous_request_ids": list(k.get("previous_request_ids", ()))})
    return sent, scores, rewrites


def warm_proof(args, driver):
    from agent.system_prompt import build_system_prompt
    agent, db, route = driver.build_agent(args["cwd"])
    try:
        prompt = build_system_prompt(agent)
        prompt += "\n\n" + (agent.ephemeral_system_prompt or "")
        home_soul = Path(os.environ["HERMES_HOME"]) / "SOUL.md"
        expected = home_soul.read_text()
        if sha(home_soul) != args["soul_sha256"] or expected not in prompt:
            raise RuntimeError("frozen SOUL missing from system prompt")
        if args["arm"] == "eva" and (Path(args["inputs"]) / "voice.md").read_text() not in prompt:
            raise RuntimeError("frozen voice missing from system prompt")
        hits = driver.canaries(prompt)
        if any(hits.values()):
            raise RuntimeError("warm negative canary")
        return {"soul_sha256": sha(home_soul), "voice_in_system": args["arm"] == "eva", "route": route}
    finally:
        agent.close()
        if db is not None:
            db.close()


def conversations(agent, messages, observe):
    """One agent across three real run_conversation calls; do not close between turns."""
    previous, turns = None, []
    for index, message in enumerate(messages):
        result = agent.run_conversation(message, **({"conversation_history": previous} if previous is not None else {}))
        turns.append(observe(index, result))
        previous = result.get("messages")
        if not result.get("completed") or result.get("failed") or result.get("error"):
            raise RuntimeError("Hermes turn failed")
    return turns


def render_pair(score, recorded, render):
    cued, stripped = render(score, "eleven_v4").text, render(score, "tagless").text
    if cued != recorded:
        raise RuntimeError("comparison-a cued render differs from recorded provider input")
    return cued, stripped


def child(args, driver):
    """NEW turn.py modes. This code is imported from the archived tagged chip, never the live checkout."""
    os.environ["BENCH_PROBE_EXCLUDE_FILES"] = json.dumps([str(Path(args["inputs"]) / name)
                                                        for name in ("SOUL.md", "voice.md")])
    os.environ["BENCH_PROBE_OUT"] = str(Path(args["item_dir"]) / "probe.jsonl")
    os.environ["BENCH_PROBE_ITEM"] = args["item_dir"]
    # The parent never receives the key, even in Host's constructor or log scrubber.
    os.environ["ELEVENLABS_API_KEY"] = run.key_value(args["key_file"], "ELEVENLABS_API_KEY")
    try:
        if args["mode"] == "e7-warm":
            return warm_proof(args, driver)
        if args["mode"] == "e7-render":
            from emotion_chip.hermes.eleven import ElevenClient
            from emotion_chip.render import render
            cued, stripped = render_pair(args["score"], args["recorded"], render)
            client = ElevenClient()
            clips = []
            for label, text in (("cued", cued), ("stripped", stripped)):
                dest = Path(args["item_dir"]) / "out" / (label + ".mp3")
                client.synthesize(text, dest, voice=args["voice"], model="eleven_v4", params=VOICE_SETTINGS,
                                  seed=args["seed"])
                clips.append(str(dest))
            return {"clips": clips, "cued_differs": cued != stripped}
        from hermes_cli.oneshot import _close_agent
        agent, db, route = driver.build_agent(args["cwd"])
        sent, scores, rewrites = install_capture(args, driver)
        relays = driver.wrap_upstream(args["item_dir"])
        cursor = [0]

        def observe(index, result):
            rows = sent[cursor[0]:]
            cursor[0] = len(sent)
            if index and (not rows or not rows[0].get("previous_request_ids")):
                raise RuntimeError("mini-session missing stitching ids")
            calls, results = driver.tts_calls(result.get("messages"))
            return {"sent_to_voice": rows, "tts_calls": calls, "tts_results": results}
        try:
            turns = conversations(agent, args["prompts"], observe)
        finally:
            _close_agent(agent, db, getattr(agent, "session_id", None))
            driver.stop_natives()
        return {"turns": turns, "scores": scores, "sent_to_voice": sent, "rewrites": rewrites,
                "completed": True, "route": route, "relays": relays}
    except Exception as exc:
        # Never let SDK/provider exceptions print credential values or private text to the parent.
        key = os.environ["ELEVENLABS_API_KEY"]
        message = str(exc).replace(key, "[redacted]")
        credential = bool(run.CREDENTIAL_FAILURE.search(message))
        return {"completed": False, "error_type": type(exc).__name__, "credential_failure": credential}
    finally:
        os.environ.pop("ELEVENLABS_API_KEY", None)


def tone(dest):
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
               "-af", "volume=-20dB", "-ar", "44100", "-b:a", "128k", "-map_metadata", "-1",
               "-id3v2_version", "0", "-write_id3v1", "0", "-write_xing", "0", str(dest)]
    subprocess.run(command, capture_output=True, check=True)


def dry_cell(session, item, arm, messages, seed):
    """Fixtures exercise the same ledger and bundle path without importing Hermes."""
    folder = session / "cells" / item / arm
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for index, _ in enumerate(messages):
        dest = folder / f"tone-{secrets.token_hex(4)}.mp3"
        tone(dest)
        clips.append(str(dest.relative_to(session)))
    if item in prompts()["comparison_a"]:
        second = folder / ("stripped-" + secrets.token_hex(4) + ".mp3")
        shutil.copyfile(session / clips[0], second)
        clips.append(str(second.relative_to(session)))
    return {"status": "completed", "clips": clips, "cued_differs": True,
            "proof": {"fake": True, "seed": seed, "previous_request_ids": [[], ["fake-1"], ["fake-1", "fake-2"]]
                      if len(messages) == 3 else [[]]}}


def real_cell(session, item, arm, messages, seed, paths, attempt):
    folder = session / "cells" / item / arm / f"attempt-{attempt}"
    soul_sha = build_home(folder, arm, session, paths, seed)
    host = E7Host(session, "", paths)
    base = {"item_dir": str(folder), "cwd": str(session / "cwd"), "arm": arm, "soul_sha256": soul_sha}
    from bench.gen import keychain_token
    token = keychain_token()
    warm = host.call(folder, dict(base, mode="e7-warm"), token=token)
    if "error" in warm or warm.get("soul_sha256") != soul_sha:
        return {"status": "infra_failure", "clips": [], "proof": {"warm": warm}}
    proof = host.call(folder, dict(base, mode="e7-turn", prompts=messages), token=token)
    del token
    if proof.get("credential_failure") or run.credential_failure(proof):
        return {"status": "credential_failure", "clips": [], "proof": proof}
    if "rc" in proof:
        return {"status": "infra_failure", "clips": [], "proof": proof}
    if not proof.get("completed"):
        return {"status": "failed", "clips": [], "proof": proof}
    pre = run.read_jsonl(folder / "probe.jsonl")
    upstream = run.read_jsonl(folder / "upstream.jsonl")
    if not pre or not upstream or any(any(r.get("canaries", {}).values()) for r in pre + upstream):
        return {"status": "failed", "clips": [], "proof": proof, "integrity": "missing/negative canary evidence"}
    rows = proof["sent_to_voice"]
    clips = [str((folder / "out" / r["clip"]).relative_to(session)) for r in rows if r.get("clip")]
    result = {"status": "completed" if len(clips) == len(messages) else "unvoiced", "clips": clips, "proof": proof}
    if item in prompts()["comparison_a"] and result["status"] == "completed":
        if len(proof["scores"]) != 1 or len(rows) != 1:
            raise RuntimeError("comparison-a requires one authored score and one provider call")
        rendered = host.call(folder, dict(base, mode="e7-render", score=proof["scores"][0], recorded=rows[0]["text"],
                                         seed=seed, voice=paths["ava_voice_id"]))
        if len(rendered.get("clips", [])) != 2:
            return {"status": "infra_failure", "clips": [], "proof": {"turn": proof, "render": rendered}}
        result.update(clips=[str(Path(p).relative_to(session)) for p in rendered["clips"]],
                      cued_differs=rendered["cued_differs"])
    return result


def jobs(reserve=False):
    data = prompts()
    for comparison in (() if reserve else ("comparison_a", "comparison_b")):
        for item, text in data[comparison].items():
            for arm in (("chip",) if comparison == "comparison_a" else ("chip", "eva")):
                yield item, arm, [text + " " + run.SUFFIX]
    for index, (item, text) in enumerate(data["catch_reserve" if reserve else "catch"].items()):
        yield item, "chip" if index < 2 else "eva", [text + " " + run.SUFFIX]
    if not reserve:
        for item, texts in data["mini_sessions"].items():
            yield item, "chip", [t + " " + run.SUFFIX for t in texts]


def generate(tag, session, *, dry_run=False, paths=None, retry_infra=None, reason=None, reserve=False):
    session = Path(session).resolve()
    paths = paths or load_paths()
    commit = frozen_tag(tag, paths["chip_repo"])
    session.mkdir(parents=True, exist_ok=True)
    manifest_path = session / "manifest.json"
    manifest = read(manifest_path) if manifest_path.exists() else {
        "version": 2, "tag": tag, "commit": commit, "dry_run": dry_run, "cells": {},
        "seeds": {item: seed_for(item) for group in prompts().values() for item in group},
        "routes": {"sag": {"normalize": "off", "lang": "en", "endpoint": "sag speak /v1/text-to-speech/{voice}"},
                   "chip": {"normalize": None, "lang": None, "endpoint": "/v1/text-to-speech/{voice}"}},
        "voice_settings": VOICE_SETTINGS, "model": run.MODEL, "effort": "high", "provider": run.PROVIDER}
    if (manifest["tag"], manifest["commit"], manifest["dry_run"]) != (tag, commit, dry_run):
        raise ValueError("session generation identity changed")
    manifest["inputs_sha256"] = snapshot(session, paths, manifest.get("inputs_sha256"))
    # Validate wrapper even in dry-run; an irreproducible baseline stops before any cell.
    seeded_wrapper((session / "inputs/sag-original.sh").read_text(), paths["sag_binary"], paths["ava_voice_id"])
    if not dry_run and not (session / "src").exists():
        manifest["shas"] = run.prepare(session, "", paths, ref=commit)
    elif dry_run and not (session / "src").exists():
        run.archive(paths["chip_repo"], commit, session / "src/emotion-chip")
    plugin_tree = run.tree_sha256(session / "src/emotion-chip")
    if manifest.get("plugin_tree") and manifest["plugin_tree"] != plugin_tree:
        raise ValueError("archived plugin tree changed")
    manifest["plugin_tree"] = plugin_tree
    if retry_infra and not reason:
        raise ValueError("--retry-infra requires --reason")
    if retry_infra:
        prior = [a for key, attempts in manifest["cells"].items() if key.split(":")[0] == retry_infra for a in attempts]
        if not prior or not any(a["status"] == "infra_failure" for a in prior):
            raise ValueError("retry requires a recorded infrastructure failure")
    write(manifest_path, manifest)
    for item, arm, messages in jobs(reserve):
        key = item + ":" + arm
        attempts = manifest["cells"].setdefault(key, [])
        if attempts:
            if item != retry_infra or attempts[-1]["status"] != "infra_failure":
                continue
        elif retry_infra:
            continue
        attempts.append({"status": "started", "reason": reason if retry_infra else None, "clips": []})
        write(manifest_path, manifest)  # first render, including interrupted attempts, is retained
        try:
            result = dry_cell(session, item, arm, messages, manifest["seeds"][item]) if dry_run else real_cell(
                session, item, arm, messages, manifest["seeds"][item], paths, len(attempts))
        except (OSError, subprocess.SubprocessError):
            result = {"status": "infra_failure", "clips": []}
        attempts[-1].update(result)
        attempts[-1]["reason"] = reason if retry_infra else None
        attempts[-1]["clip_sha256"] = [sha(session / p) for p in result["clips"]]
        write(manifest_path, manifest)
        if result["status"] in {"credential_failure", "failed", "infra_failure"}:
            raise RuntimeError("generation stopped after " + result["status"])
    return manifest


def completed(manifest, item, arm, session):
    attempts = manifest["cells"].get(item + ":" + arm, [])
    good = [a for a in attempts if a["status"] == "completed"]
    if len(good) != 1:
        raise ValueError("missing item or two completed attempts")
    row = good[0]
    clips = [Path(session) / p for p in row["clips"]]
    if not clips or [sha(p) for p in clips] != row["clip_sha256"]:
        raise ValueError("recorded clip bytes changed")
    return row, clips


def measure(clip):
    command = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(clip), "-af",
               "loudnorm=I=-18:TP=-1:LRA=11:print_format=json", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    matches = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr, re.S)
    if not matches:
        raise ValueError("missing loudness measurement")
    data = json.loads(matches[-1])
    loudness, peak = float(data["input_i"]), float(data["input_tp"])
    if not math.isfinite(loudness) or not math.isfinite(peak):
        raise ValueError("silent or invalid clip loudness")
    return {"lufs": loudness, "peak": peak, "command": command}


def gains(measurements):
    excess = max([0.0] + [m["peak"] - 18 - m["lufs"] + 1 for m in measurements])
    target = -18 - excess
    return target, [target - m["lufs"] for m in measurements]


def normalize(clip, dest, gain, trace=None):
    """Calibrate encoder loss, then encode FROM THE ORIGINAL with one constant gain.

    The disposable encode is not a second synthesis and never enters the listening set.
    No dynamic filter or lossy-to-lossy correction chain is used.
    """
    def command(output, value):
        return ["ffmpeg", "-v", "error", "-i", str(clip), "-af", f"volume={value:.8f}dB",
                "-ar", "44100", "-b:a", "128k", "-map_metadata", "-1", "-id3v2_version", "0",
                "-write_id3v1", "0", "-write_xing", "0", str(output)]
    source = measure(clip)
    with tempfile.TemporaryDirectory(prefix="e7-encode-") as scratch:
        probe = Path(scratch) / "probe.mp3"
        calibration = command(probe, gain)
        subprocess.run(calibration, capture_output=True, check=True)
        encoded = measure(probe)
        corrected = gain + source["lufs"] + gain - encoded["lufs"]
        if trace is not None:
            trace.extend([source["command"], calibration, encoded["command"]])
    final = command(dest, corrected)
    subprocess.run(final, capture_output=True, check=True)
    return final


def check_mp3(path):
    """Inspect tags and the FIRST MPEG frame only; audio frames are never text-scanned."""
    data = Path(path).read_bytes()
    if data.startswith(b"ID3") or data[-128:-125] == b"TAG":
        raise ValueError("MP3 ID3 metadata")
    if len(data) < 4 or data[0] != 255 or data[1] & 0xE0 != 0xE0:
        raise ValueError("invalid MP3 header")
    version, layer = (data[1] >> 3) & 3, (data[1] >> 1) & 3
    rate_index, sample_index, padding = data[2] >> 4, (data[2] >> 2) & 3, (data[2] >> 1) & 1
    if version != 3 or layer != 1 or rate_index not in range(1, 15) or sample_index == 3:
        raise ValueError("expected MPEG1 Layer III")
    rate = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320][rate_index]
    length = 144000 * rate // [44100, 48000, 32000][sample_index] + padding
    first = data[:length]
    side = 17 if (data[3] >> 6) == 3 else 32
    offset = 4 + (0 if data[1] & 1 else 2) + side
    if first[offset:offset + 4] in (b"Xing", b"Info"):
        flags = int.from_bytes(first[offset + 4:offset + 8], "big")
        if flags & ~15:
            raise ValueError("unknown Xing fields")
        end = offset + 8 + sum(size for bit, size in ((1, 4), (2, 4), (4, 100), (8, 4)) if flags & bit)
        trailer = first[end:]
        encoder = trailer[:9]
        if encoder and not re.fullmatch(rb"(?:LAME|Lavc|Lavf)[\w. -]*\x00*", encoder):
            raise ValueError("unexpected encoder metadata")
        if any(trailer[9:]):
            raise ValueError("Xing/LAME header carries extra metadata")


def blind_scan(bundle, forbidden):
    for path in Path(bundle).iterdir():
        if path.suffix == ".mp3":
            check_mp3(path)
        else:
            data = path.read_bytes().lower()
            if any(str(token).encode().lower() in data for token in forbidden if str(token)):
                raise ValueError("blind bundle contains identifying text")


def pairs_from(manifest, session, reserve=False):
    pairs = []
    data = prompts()
    for comparison in ("comparison_a", "comparison_b"):
        for item in data[comparison]:
            chip, clips = completed(manifest, item, "chip", session)
            arms = {"cued": clips[0], "stripped": clips[1]} if comparison == "comparison_a" else {
                "chip": clips[0], "eva": completed(manifest, item, "eva", session)[1][0]}
            if comparison == "comparison_a" and len(clips) != 2:
                raise ValueError("comparison-a missing render")
            pairs.append({"kind": "real", "comparison": comparison, "item": item, "arms": arms,
                          "seed": manifest["seeds"][item], "cued_differs": chip.get("cued_differs")})
    for index, item in enumerate(data["catch_reserve" if reserve else "catch"]):
        arm = "chip" if index < 2 else "eva"
        clip = completed(manifest, item, arm, session)[1][0]
        pairs.append({"kind": "catch", "comparison": None, "item": item, "arms": {arm: clip, "duplicate": clip},
                      "seed": manifest["seeds"][item], "cued_differs": None})
    for item in data["mini_sessions"]:
        clips = completed(manifest, item, "chip", session)[1]
        if len(clips) != 3:
            raise ValueError("missing mini-session turn")
        pairs.append({"kind": "mini", "comparison": None, "item": item, "arms": {}, "clips": clips,
                      "seed": manifest["seeds"][item], "cued_differs": None})
    return pairs


def rerun_admission(parent, session, now=None):
    source = read(parent / "manifest.json")
    if source.get("rerun_of"):
        raise ValueError("cannot rerun a rerun")
    if not source.get("locked_at"):
        raise ValueError("first session has no recorded lock")
    locked = datetime.fromisoformat(source["locked_at"].replace("Z", "+00:00"))
    if ((now or datetime.now(timezone.utc)) - locked).total_seconds() < 12 * 3600:
        raise ValueError("rerun requires 12 hours after first lock")
    receipt = parent / "rerun.json"
    if receipt.exists():
        if read(receipt)["session"] != str(session):
            raise ValueError("first session already has a rerun")
    else:
        write(receipt, {"session": str(session)}, exclusive=True)
    return source


def build(session, bundle, key_path, *, rerun_of=None, now=None):
    session, bundle, key_path = (Path(p).resolve() for p in (session, bundle, key_path))
    if key_path == bundle or key_path.is_relative_to(bundle) or session == bundle or bundle.is_relative_to(session / "inputs"):
        raise ValueError("key and private inputs must be outside bundle")
    if key_path.exists():
        raise ValueError("key already exists")
    if bundle.exists() and any(bundle.iterdir()):
        raise ValueError("bundle is not empty")
    source_key = None
    if rerun_of:
        parent = Path(rerun_of).resolve()
        if parent == session:
            raise ValueError("rerun needs a fresh session")
        source = rerun_admission(parent, session, now)
        source_key_path = Path(source["key_path"])
        if sha(source_key_path) != source["key_sha256"]:
            raise ValueError("first key commitment changed")
        source_key = read(source_key_path)
        session.mkdir(parents=True, exist_ok=True)
        if not (session / "manifest.json").exists():
            shutil.copytree(parent / "inputs", session / "inputs")
            manifest = {k: v for k, v in source.items() if k not in {"cells", "key_sha256", "bundle", "key_path",
                        "bundle_path", "locked_at", "ratings_sha256", "score", "shas"}}
            manifest.update(cells={}, rerun_of=str(parent))
            write(session / "manifest.json", manifest, exclusive=True)
        # No live persona read on reruns: the same four snapshots are the only sources.
        paths = load_paths()
        paths.update({k: session / "inputs" / name for k, name in INPUTS.items()})
        generate(source["tag"], session, dry_run=source["dry_run"], paths=paths, reserve=True)
        manifest = read(session / "manifest.json")
        # Reuse normalized real and mini bytes, never a second lossy encode.
        pairs = []
        for trial in source_key["trials"]:
            if trial["kind"] == "catch":
                continue
            row = {k: trial[k] for k in ("kind", "comparison", "item", "seed", "cued_differs")}
            files = trial["files"]
            for name, digest in trial["clip_sha256"].items():
                if sha(Path(source["bundle_path"]) / name) != digest:
                    raise ValueError("first bundle bytes changed")
            row["arms"] = {arm: Path(source["bundle_path"]) / files[side] for side, arm in trial["sides"].items()}
            if trial["kind"] == "mini":
                row["clips"] = [Path(source["bundle_path"]) / name for name in files]
            row["prior_gains"] = ({trial["sides"][side]: gain for side, gain in trial["gains"].items()}
                                  if trial["kind"] != "mini" else trial["gains"])
            pairs.append(row)
        for index, item in enumerate(prompts()["catch_reserve"]):
            arm = "chip" if index < 2 else "eva"
            clip = completed(manifest, item, arm, session)[1][0]
            pairs.append({"kind": "catch", "comparison": None, "item": item, "seed": manifest["seeds"][item],
                          "cued_differs": None, "arms": {arm: clip, "duplicate": clip}})
    else:
        manifest = read(session / "manifest.json")
        if manifest.get("rerun_of"):
            raise ValueError("rerun requires --rerun-of")
        pairs = pairs_from(manifest, session)
    if input_hashes(session / "inputs") != manifest["inputs_sha256"]:
        raise ValueError("frozen inputs changed")
    sources = list(dict.fromkeys(p for row in pairs for p in (row.get("clips") or list(row["arms"].values()))))
    measurements = {p: measure(p) for p in sources}
    target, values = gains(list(measurements.values()))
    gain_map = dict(zip(sources, values))
    if source_key and target < source_key["target_lufs"] - .05:
        raise ValueError("reserve catch would lower target; cannot preserve real bytes")
    if source_key:
        target = source_key["target_lufs"]
        gain_map = {p: target - m["lufs"] for p, m in measurements.items()}
    bundle.mkdir(parents=True, exist_ok=True)
    rng = secrets.SystemRandom()
    rng.shuffle(pairs)
    collection = "e7-" + secrets.token_hex(6)
    while any(item in collection for group in prompts().values() for item in group):
        collection = "e7-" + secrets.token_hex(6)
    key = {"version": 2, "collection": collection, "target_lufs": target, "trials": [],
           "dry_run": manifest["dry_run"], "rerun_of": str(rerun_of) if rerun_of else None}
    public = []
    pair_index, mini_index = 0, 0
    for row in pairs:
        record = {k: row[k] for k in ("kind", "comparison", "item", "seed", "cued_differs")}
        if row["kind"] == "mini":
            mini_index += 1
            trial_id = f"m{mini_index:02d}"
            mapping = {str(i): p for i, p in enumerate(row["clips"], 1)}
            names = {side: f"{trial_id}-{side}.mp3" for side in mapping}
            record["sides"] = {}
            record["files"] = list(names.values())
            public.append({"id": trial_id, "clips": list(names.values())})
        else:
            pair_index += 1
            trial_id = f"t{pair_index:02d}"
            arms = list(row["arms"])
            rng.shuffle(arms)
            record["sides"] = dict(zip(("a", "b"), arms))
            mapping = {side: row["arms"][arm] for side, arm in record["sides"].items()}
            names = {side: f"{trial_id}-{side}.mp3" for side in mapping}
            record["files"] = names
            public.append({"id": trial_id, **names})
        record.update(id=trial_id, gains={}, commands={}, clip_sha256={})
        first_catch = None
        first_catch_gain = None
        for side, src in mapping.items():
            dest = bundle / names[side]
            calibration = []
            if row["kind"] == "catch" and first_catch:
                shutil.copyfile(first_catch, dest)
                command = ["byte-identical-copy"]
            elif "prior_gains" in row:
                shutil.copyfile(src, dest)
                command = ["byte-identical-copy"]
            else:
                command = normalize(src, dest, gain_map[src], calibration)
            prior_side = record["sides"].get(side, side)
            applied = (float(command[command.index("-af") + 1].removeprefix("volume=").removesuffix("dB"))
                       if "-af" in command else row.get("prior_gains", {}).get(prior_side, first_catch_gain))
            record["gains"][side] = applied
            if row["kind"] == "catch":
                first_catch, first_catch_gain = dest, applied
            record["commands"][side] = {"analysis": measurements[src]["command"], "calibration": calibration, "encode": command}
            record["clip_sha256"][names[side]] = sha(dest)
        key["trials"].append(record)
    page = (HERE / "template.html").read_text().replace('/*TRIALS*/[]', json.dumps(public)).replace(
        '/*STORE*/"ratings"', json.dumps(collection))
    (bundle / "index.html").write_text(page, encoding="utf-8")
    forbidden = ["chip", "eva", "cued", "stripped", "comparison_a", "comparison_b", "seed", str(session), str(ROOT),
                 *manifest["seeds"].values(),
                 *[item for group in prompts().values() for item in group], *[str(p) for p in sources]]
    blind_scan(bundle, forbidden)
    for path in bundle.iterdir():
        os.utime(path, (FIXED_MTIME, FIXED_MTIME))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    write(key_path, key, exclusive=True)
    manifest.update(key_sha256=sha(key_path), key_path=str(key_path), bundle_path=str(bundle),
                    bundle={p.name: sha(p) for p in sorted(bundle.iterdir())}, collection=collection)
    write(session / "manifest.json", manifest)
    return key
