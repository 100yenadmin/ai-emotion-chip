"""Offline E7 integrity rules 1–11: fakes for Hermes; real ffmpeg on synthetic tones only."""
from datetime import datetime, timezone
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from bench.e7 import session as e7
from bench.e7 import score as scoring
from bench.e7.__main__ import main
from bench.hermes import run, turn
from bench.hermes.bench_probe import probe
from emotion_chip.hermes.eleven import ElevenClient
from emotion_chip.hermes.provider import EmotionChipTTSProvider
from emotion_chip.hermes.settings import DEFAULTS, Settings
from emotion_chip.render import render
from fakes import FakeContext


@pytest.fixture(autouse=True)
def isolated_child_environment(monkeypatch):
    for name in ("BENCH_PROBE_EXCLUDE_FILES", "BENCH_PROBE_OUT", "BENCH_PROBE_ITEM"):
        monkeypatch.setenv(name, "")


WRAPPER = '''#!/bin/sh
set -eu
if [ "$#" -ne 3 ]; then
  exit 64
fi
input_path=$1
output_path=$2
hermes_format=$3
case "$hermes_format" in
  mp3) sag_format=mp3_44100_128 ;;
  *) exit 65 ;;
esac
exec {binary} speak \\
  -f "$input_path" \\
  -o "$output_path" \\
  -v fixture-ava \\
  --model-id eleven_v4 \\
  --format "$sag_format" \\
  --speed 0.90 \\
  --stability 0.5 \\
  --similarity 0.75 \\
  --style 0.30 \\
  --normalize off \\
  --lang en \\
  --no-play
'''


def fixture_paths(root):
    root.mkdir(parents=True, exist_ok=True)
    values = {k: root / name for k, name in e7.INPUTS.items()}
    values["eva_soul"].write_text("A fixture persona. AGENTS.md\n")
    values["eva_voice"].write_text("Use a fixture voice.\n")
    values["eva_skill_dir"].mkdir()
    (values["eva_skill_dir"] / "SKILL.md").write_text("---\nname: speech-authorship\n---\nFixture guidance.\n")
    binary = root / "sag"
    values["eva_sag_wrapper"].write_text(WRAPPER.format(binary=binary))
    values.update(sag_binary=binary, ava_voice_id="fixture-ava", chip_repo=e7.ROOT, canaries=["private-marker"],
                  eleven_key_file=root / "unread-key-file")
    return values


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("e7")
    paths = fixture_paths(root / "source")
    session = root / "session"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e7, "frozen_tag", lambda tag, repo: "fixture-commit")
        def archive(repo, ref, dest):
            assert ref == "fixture-commit"
            dest.mkdir(parents=True)
            (dest / "archived.txt").write_text(ref)
        patch.setattr(run, "archive", archive)
        e7.generate("fixture-tag", session, paths=paths, dry_run=True)
    return root, paths, session


@pytest.fixture(scope="module")
def built(generated):
    root, paths, original = generated
    # Build on a copy: a built session refuses a second bundle, and other tests copy the unbuilt one.
    session = root / "built-session"
    shutil.copytree(original, session)
    key_path, bundle = root / "key.json", root / "bundle"
    key = e7.build(session, bundle, key_path)
    return root, paths, session, bundle, key_path, key


def ratings_for(key, wins=15, catches=0):
    counts = {"comparison_a": 0, "comparison_b": 0}
    docs = {}
    for trial in key["trials"]:
        choice = "none"
        if trial["kind"] == "real":
            comparison = trial["comparison"]
            target = "cued" if comparison == "comparison_a" else "chip"
            if counts[comparison] < wins:
                choice = next(side for side, arm in trial["sides"].items() if arm == target)
            counts[comparison] += 1
        elif trial["kind"] == "catch" and catches:
            choice = "a"
            catches -= 1
        doc = {"trial": trial["id"], "choice": choice, "acting": {"a": False, "b": False},
               "note": "private note", "updatedAt": "2026-10-22T11:59:00Z"}
        if trial["kind"] == "mini":
            doc["rating"] = 4
        docs[trial["id"]] = doc
    docs["lock"] = {"lockedAt": "2026-10-22T12:00:00Z", "ratings": deepcopy(docs)}
    return {"docs": docs}


def test_rule1_tag_namespace_clean_head_and_prepare_ref(monkeypatch, tmp_path):
    calls = []
    def rev(repo, ref):
        calls.append(ref)
        if ref == "refs/tags/branch":
            raise subprocess.CalledProcessError(1, "git")
        return "commit"
    monkeypatch.setattr(run, "rev", rev)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    assert e7.frozen_tag("release", tmp_path, tmp_path) == "commit"
    assert calls == ["refs/tags/release", "HEAD", "HEAD"]
    with pytest.raises(ValueError, match="branches"):
        e7.frozen_tag("branch", tmp_path, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=" M tracked"))
    with pytest.raises(ValueError, match="dirty"):
        e7.frozen_tag("release", tmp_path, tmp_path)
    monkeypatch.setattr(run, "rev", lambda repo, ref: "other" if ref == "HEAD" else "commit")
    with pytest.raises(ValueError, match="HEAD"):
        e7.frozen_tag("release", tmp_path, tmp_path)


def test_rule1_prepare_archives_resolved_ref(tmp_path, monkeypatch):
    paths = {}
    for name in ("fish_repo", "chip_repo", "provider_src", "host"):
        paths[name] = tmp_path / name
        paths[name].mkdir()
    paths["host_label"] = "fixture host"
    calls = []
    monkeypatch.setattr(run, "rev", lambda repo, ref: "resolved-" + ref)
    def archive(repo, ref, dest):
        calls.append((repo, ref))
        dest.mkdir(parents=True)
    monkeypatch.setattr(run, "archive", archive)
    dest = tmp_path / "run"
    dest.mkdir()
    result = run.prepare(dest, "", paths, ref="tag-commit")
    assert (paths["chip_repo"], "resolved-tag-commit") in calls
    assert result["emotion_chip"] == "tag-commit resolved-tag-commit"


def test_rule2_snapshot_refuses_different_inputs_and_tampering(tmp_path):
    paths = fixture_paths(tmp_path / "source")
    folder = tmp_path / "session"
    folder.mkdir()
    hashes = e7.snapshot(folder, paths)
    assert e7.snapshot(folder, paths, hashes) == hashes
    paths["eva_voice"].write_text("changed")
    with pytest.raises(ValueError, match="snapshot"):
        e7.snapshot(folder, paths, hashes)


def test_rule2_seeded_wrapper_only_two_changes(tmp_path):
    paths = fixture_paths(tmp_path / "source")
    original = paths["eva_sag_wrapper"].read_text()
    seeded = e7.seeded_wrapper(original, paths["sag_binary"], "fixture-ava")
    assert seeded.replace('"$#" -ne 4', '"$#" -ne 3').replace('--seed "$4" \\\n  ', '') == original
    with pytest.raises(ValueError, match="flags"):
        e7.seeded_wrapper(original.replace("--normalize off", "--normalize on"), paths["sag_binary"], "fixture-ava")


def test_rule3_canary_exclusion_opt_in_and_exact_bytes(tmp_path, monkeypatch):
    frozen = "Persona AGENTS.md\nsecond line"
    path = tmp_path / "SOUL.md"
    path.write_text(frozen)
    assert probe.canaries(frozen)["AGENTS.md"]
    monkeypatch.setenv("BENCH_PROBE_EXCLUDE_FILES", json.dumps([str(path)]))
    assert not probe.canaries(frozen)["AGENTS.md"]
    assert not probe.canaries(json.dumps({"system": frozen}))["AGENTS.md"]
    assert probe.canaries(frozen + " AGENTS.md")["AGENTS.md"]
    monkeypatch.delenv("BENCH_PROBE_EXCLUDE_FILES")
    assert probe.canaries(frozen)["AGENTS.md"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("seed", [None, 0, 4294967295])
def test_rule4_provider_seed_top_level_and_unset_unchanged(tmp_path, monkeypatch, stream, seed):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "offline-fixture")
    calls = []
    class Response(BytesIO):
        headers = {"request-id": "fixture"}
    def opener(request, **kwargs):
        calls.append(json.loads(request.data))
        return Response(b"fixture audio")
    ctx = FakeContext({"backend": "eleven", "eleven_voice_id": "fixture", "eleven_seed": seed})
    provider = EmotionChipTTSProvider(Settings(ctx), data_dir=lambda: tmp_path, opener=opener)
    if stream:
        list(provider.stream("Hello."))
    else:
        provider.synthesize("Hello.", tmp_path / "audio.mp3")
    if seed is None:
        assert "seed" not in calls[0]
    else:
        assert calls[0]["seed"] == seed
        assert "seed" not in calls[0]["voice_settings"]
    assert DEFAULTS["eleven_seed"] is None
    schema = yaml.safe_load((e7.ROOT / "plugin.yaml").read_text())["config_schema"]["eleven_seed"]
    assert schema["default"] is None and schema["type"] == "int"


def test_rule4_prompt_pin_and_seed_order(generated):
    _, _, folder = generated
    assert e7.sha(e7.HERE / "prompts.json") == "8d8498b85d1e3525e9019e3a5bbed0ec2945d451b0b630cd21d2d034702b534c"
    expected = {item: __import__("random").Random(f"{e7.SEED_ROOT}:{item}").randrange(2**32)
                for group in e7.prompts().values() for item in group}
    assert e7.read(folder / "manifest.json")["seeds"] == expected
    assert e7.seed_for("a01") == expected["a01"]


def test_comparison_a_uses_provider_score_and_checks_render():
    assert e7.render_pair("[soft: Hello.]", "[softly] Hello.", render) == ("[softly] Hello.", "Hello.")
    with pytest.raises(RuntimeError, match="recorded provider input"):
        e7.render_pair("[soft: Hello.]", "Hello.", render)
    assert e7.render_pair("Plain.", "Plain.", render) == ("Plain.", "Plain.")


def test_rule5_no_overwrite_and_retry_preserves_attempts(generated, tmp_path, monkeypatch):
    _, paths, source = generated
    folder = tmp_path / "session"
    shutil.copytree(source, folder)
    before = e7.read(folder / "manifest.json")
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    monkeypatch.setattr(e7, "dry_cell", lambda *a: pytest.fail("recorded cell was regenerated"))
    e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    assert e7.read(folder / "manifest.json") == before
    with pytest.raises(ValueError, match="recorded failure"):
        e7.generate("fixture-tag", folder, dry_run=True, paths=paths, retry="a01", reason="outage")
    before["cells"]["a01:chip"] = [{"status": "turn_error", "clips": [], "reason": "turn_error"}]
    e7.write(folder / "manifest.json", before)
    good = e7.read(source / "manifest.json")["cells"]["a01:chip"][0]
    monkeypatch.setattr(e7, "dry_cell", lambda *a: good)
    e7.generate("fixture-tag", folder, dry_run=True, paths=paths, retry="a01", reason="network recovered")
    attempts = e7.read(folder / "manifest.json")["cells"]["a01:chip"]
    assert [a["status"] for a in attempts] == ["turn_error", "completed"]
    assert attempts[1]["retry_reason"] == "network recovered"


def test_rule5_missing_duplicate_and_unvoiced_refused(generated):
    _, _, folder = generated
    manifest = e7.read(folder / "manifest.json")
    row = manifest["cells"].pop("a01:chip")
    with pytest.raises(ValueError, match="missing"):
        e7.pairs_from(manifest, folder)
    manifest["cells"]["a01:chip"] = row * 2
    with pytest.raises(ValueError, match="two completed"):
        e7.pairs_from(manifest, folder)
    manifest["cells"]["a01:chip"] = [{"status": "unvoiced", "clips": []}]
    with pytest.raises(ValueError, match="missing"):
        e7.pairs_from(manifest, folder)


def test_rule6_bundle_blind_names_mtimes_and_catch_bytes(built):
    _, _, _, bundle, _, key = built
    names = {p.name for p in bundle.iterdir()}
    assert len(names) == 98
    assert all(name == "index.html" or __import__("re").fullmatch(r"(?:t\d{2}-[ab]|m\d{2}-[123])\.mp3", name) for name in names)
    assert {p.stat().st_mtime for p in bundle.iterdir()} == {e7.FIXED_MTIME}
    e7.blind_scan(bundle, ["chip", "eva", "comparison", "seed", "a01", "b01"])
    for trial in key["trials"]:
        if trial["kind"] == "catch":
            assert (bundle / trial["files"]["a"]).read_bytes() == (bundle / trial["files"]["b"]).read_bytes()


def test_rule6_text_and_id3_leaks_fail(tmp_path, generated):
    (tmp_path / "index.html").write_text("comparison_a")
    with pytest.raises(ValueError, match="identifying"):
        e7.blind_scan(tmp_path, ["comparison_a"])
    audio = next(generated[2].rglob("*.mp3")).read_bytes()
    for data in (b"ID3" + audio, audio + b"TAG" + bytes(125)):
        path = tmp_path / "tagged.mp3"
        path.write_bytes(data)
        with pytest.raises(ValueError, match="ID3"):
            e7.check_mp3(path)
    # An audio-frame byte sequence is not metadata and must never be word-scanned.
    (tmp_path / "index.html").write_text("Listening")
    (tmp_path / "tagged.mp3").write_bytes(audio + b"comparison_a chip")
    e7.blind_scan(tmp_path, ["chip", "comparison_a"])


def test_rule7_static_gain_and_hot_clip_target_lowered(tmp_path, generated):
    source = next(generated[2].rglob("*.mp3"))
    measurement = e7.measure(source)
    target, gains = e7.gains([measurement])
    assert target == -18
    dest = tmp_path / "normalized.mp3"
    command = e7.normalize(source, dest, gains[0])
    assert command[command.index("-af") + 1].startswith("volume=")
    assert "loudnorm" not in " ".join(command)
    assert abs(e7.measure(dest)["lufs"] + 18) < 0.3
    hot = {"lufs": -30, "peak": -2}
    target, gains = e7.gains([measurement, hot])
    assert target == -29 and hot["peak"] + gains[1] <= -1
    assert measurement["peak"] + gains[0] <= -1


def test_rule8_commitments_and_key_placement(built, tmp_path):
    _, _, folder, bundle, key_path, _ = built
    manifest = e7.read(folder / "manifest.json")
    assert manifest["key_sha256"] == e7.sha(key_path)
    assert manifest["bundle"] == {p.name: e7.sha(p) for p in bundle.iterdir()}
    with pytest.raises(ValueError, match="key already"):
        e7.build(folder, tmp_path / "new-bundle", key_path)
    with pytest.raises(ValueError, match="outside"):
        e7.build(folder, tmp_path / "b", tmp_path / "b/key.json")


def test_rule9_lock_required_and_snapshot_required(built):
    _, _, folder, _, _, key = built
    manifest = e7.read(folder / "manifest.json")
    ratings = ratings_for(key)
    del ratings["docs"]["lock"]
    assert scoring.score(ratings, key, manifest) == {"status": "incomplete"}
    ratings = ratings_for(key)
    ratings["docs"]["lock"].pop("ratings")
    with pytest.raises(ValueError, match="ratings"):
        scoring.score(ratings, key, manifest)


def test_fix4_score_uses_only_locked_ratings_and_accepts_clock_skew(built, monkeypatch):
    _, _, folder, _, _, key = built
    manifest = e7.read(folder / "manifest.json")
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    ratings = ratings_for(key)
    baseline = scoring.score(ratings, key, manifest)
    assert baseline["changed_after_lock"] == 0
    trial = key["trials"][0]["id"]
    ratings["docs"][trial].update(choice="invalid", trial="wrong", note="rewritten",
                                 updatedAt="2026-10-22T12:01:00Z")
    ratings["docs"]["unrelated"] = {"trial": "unknown"}
    assert scoring.score(ratings, key, manifest) == dict(baseline, changed_after_lock=1)
    ratings["docs"]["lock"]["ratings"][trial]["updatedAt"] = "2026-10-22T13:00:00Z"
    assert scoring.score(ratings, key, manifest) == dict(baseline, changed_after_lock=1)
    # The snapshot remains scorable without any live trial documents.
    assert scoring.score({"docs": {"lock": ratings["docs"]["lock"]}}, key, manifest)["status"] == "pass"


@pytest.mark.parametrize("defect", ["missing", "not_object", "unknown_trial", "identity", "invalid", "timestamp"])
def test_fix4_locked_ratings_keep_validation(built, defect):
    _, _, folder, _, _, key = built
    ratings = ratings_for(key)
    locked = ratings["docs"]["lock"]["ratings"]
    trial = key["trials"][0]["id"]
    if defect == "missing":
        ratings["docs"]["lock"].pop("ratings")
    elif defect == "not_object":
        ratings["docs"]["lock"]["ratings"] = []
    elif defect == "unknown_trial":
        locked["unknown"] = deepcopy(locked[trial])
    elif defect == "identity":
        locked[trial]["trial"] = "other"
    elif defect == "invalid":
        locked[trial]["acting"]["a"] = "yes"
    else:
        locked[trial]["updatedAt"] = "not a timestamp"
    with pytest.raises(ValueError):
        scoring.score(ratings, key, e7.read(folder / "manifest.json"))


def test_rule10_thresholds_void_and_prereg(built, monkeypatch):
    _, _, folder, _, _, key = built
    manifest = e7.read(folder / "manifest.json")
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    result = scoring.score(ratings_for(key), key, manifest)
    assert result["status"] == "pass" and result["mini_median"] == 4
    assert result["comparisons"]["comparison_a"]["p_one_sided"] == 21700 / 1048576
    assert "claim_class" not in result  # fixtures never authorize owner-accepted claims
    assert "private note" not in json.dumps(result)
    real_key = dict(key, dry_run=False)
    assert scoring.score(ratings_for(real_key), real_key, manifest)["claim_class"] == "owner_accepted(n=1)"
    assert scoring.score(ratings_for(key, wins=14), key, manifest)["status"] == "fail"
    assert scoring.score(ratings_for(key, catches=2), key, manifest) == {"status": "void", "catch_preferences": 2}
    ratings = ratings_for(key)
    del ratings["docs"]["lock"]["ratings"][key["trials"][0]["id"]]
    assert scoring.score(ratings, key, manifest)["status"] == "incomplete"
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": False})
    with pytest.raises(ValueError, match="preregistration"):
        scoring.score(ratings_for(key), key, manifest)


def test_output_path_and_eva_capture_at_imported_function(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    writes, tool_calls = [], []
    def command(text, output, *args, **kwargs):
        Path(output).write_bytes(b"pre-conversion fixture")
        return output
    module = SimpleNamespace(_generate_command_tts=command,
                             text_to_speech_tool=lambda text, **k: tool_calls.append(k))
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(tts_tool=module))
    args = {"arm": "eva", "item_dir": str(tmp_path)}
    sent, scores, rewrites = e7.install_capture(args, turn)
    module.text_to_speech_tool("text", output_path=str(tmp_path.parent / "outside.mp3"))
    assert Path(tool_calls[0]["output_path"]).is_relative_to(tmp_path)
    assert rewrites and not scores
    output = tmp_path / "out/source.mp3"
    module._generate_command_tts("cleaned text", str(output), "e7-sag", {}, {})
    output.write_bytes(b"converted later")
    assert sent[0]["text"] == "cleaned text"
    assert (tmp_path / "out" / sent[0]["clip"]).read_bytes() == b"pre-conversion fixture"
    assert e7.rewrite_output(tmp_path, "out/in.mp3", writes) == str(tmp_path / "out/in.mp3")
    assert writes == []


def test_warm_frozen_soul_voice_and_arm_config(tmp_path, monkeypatch):
    paths = fixture_paths(tmp_path / "source")
    folder = tmp_path / "session"
    folder.mkdir()
    e7.snapshot(folder, paths)
    for name in (run.PROVIDER, "bench-probe", "emotion-chip"):
        (folder / "src" / name).mkdir(parents=True)
    for arm in ("chip", "eva"):
        item = tmp_path / arm
        digest = e7.build_home(item, arm, folder, paths, 123)
        cfg = e7.read(item / "home/config.yaml")
        assert cfg["context_file_max_chars"] >= 100000 and cfg["context_file_read_timeout"] == 600
        assert cfg["agent"]["reasoning_effort"] == "high"
        assert (item / "home/plugins/emotion-chip").exists() == (arm == "chip")
        assert (item / "home/skills/speech-authorship").exists() == (arm == "eva")
        monkeypatch.setenv("HERMES_HOME", str(item / "home"))
        monkeypatch.setenv("BENCH_PROBE_EXCLUDE_FILES", json.dumps([str(folder / "inputs/SOUL.md")]))
        prompt = (item / "home/SOUL.md").read_text()
        agent = SimpleNamespace(ephemeral_system_prompt="", close=lambda: None)
        monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
        system = SimpleNamespace(build_system_prompt=lambda agent: prompt)
        monkeypatch.setitem(sys.modules, "agent.system_prompt", system)
        driver = SimpleNamespace(build_agent=lambda cwd: (agent, None, {}), canaries=probe.canaries)
        args = {"cwd": str(folder), "arm": arm, "soul_sha256": digest, "inputs": str(folder / "inputs")}
        assert e7.warm_proof(args, driver)["soul_sha256"] == digest
        system.build_system_prompt = lambda agent: "truncated"
        with pytest.raises(RuntimeError, match="SOUL"):
            e7.warm_proof(args, driver)


def test_rule11_rerun_gate_and_byte_identical_real_pairs_and_source_counts(built, tmp_path, monkeypatch):
    _, paths, folder, bundle, key_path, first_key = built
    parent = tmp_path / "first"
    shutil.copytree(folder, parent)
    manifest = e7.read(parent / "manifest.json")
    manifest["cells"]["a01:chip"].insert(0, {"status": "synthesis_failed", "clips": []})
    manifest["attempt_counts"] = e7.attempt_counts(manifest)
    e7.write(parent / "manifest.json", manifest)
    new = tmp_path / "rerun"
    with pytest.raises(ValueError, match="lock"):
        e7.rerun_admission(parent, new)
    ratings_path = tmp_path / "void-ratings.json"
    e7.write(ratings_path, ratings_for(first_key, catches=2))
    scoring.score_files(ratings_path, key_path, parent / "manifest.json")
    manifest = e7.read(parent / "manifest.json")
    with pytest.raises(ValueError, match="12 hours"):
        e7.rerun_admission(parent, new, datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    monkeypatch.setattr(e7, "load_paths", lambda: dict(paths))
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    def archive(repo, ref, dest):
        dest.mkdir(parents=True)
        (dest / "archived.txt").write_text(ref)
    monkeypatch.setattr(run, "archive", archive)
    new_bundle, new_key = tmp_path / "new-bundle", tmp_path / "new-key.json"
    key = e7.build(new, new_bundle, new_key, rerun_of=parent,
                   now=datetime(2026, 10, 23, 0, tzinfo=timezone.utc))
    assert key["collection"] != first_key["collection"]
    rerun_manifest = e7.read(new / "manifest.json")
    assert rerun_manifest["source_attempt_counts"] == manifest["attempt_counts"]
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    result = scoring.score(ratings_for(key), key, rerun_manifest)
    assert result["source_attempt_counts"] == manifest["attempt_counts"]
    assert result["attempt_counts"] == rerun_manifest["attempt_counts"]
    assert sum(c["completed"] for c in result["attempt_counts"].values()) == 4
    original = {t["item"]: t for t in first_key["trials"]}
    assert {t["item"] for t in key["trials"] if t["kind"] == "catch"} == set(e7.prompts()["catch_reserve"])
    for trial in key["trials"]:
        if trial["kind"] != "real":
            continue
        old = original[trial["item"]]
        for side, arm in trial["sides"].items():
            old_side = next(s for s, a in old["sides"].items() if a == arm)
            assert (new_bundle / trial["files"][side]).read_bytes() == (bundle / old["files"][old_side]).read_bytes()
    with pytest.raises(ValueError, match="already has"):
        e7.rerun_admission(parent, tmp_path / "second")
    with pytest.raises(ValueError, match="rerun a rerun"):
        e7.rerun_admission(new, tmp_path / "third")
    rated = ratings_for(key, catches=2)
    assert scoring.score(rated, key, e7.read(new / "manifest.json")) == {"status": "void", "catch_preferences": 2}


def test_dry_run_cli_end_to_end(generated, tmp_path, monkeypatch, capsys):
    _, paths, original = generated
    monkeypatch.setattr(e7, "load_paths", lambda: paths)
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    folder = tmp_path / "session"
    shutil.copytree(original, folder)
    # Reusing a completed generate is part of the CLI contract: no second renders.
    main(["generate", "--tag", "fixture-tag", "--session", str(folder), "--dry-run"])
    bundle, key_path = tmp_path / "bundle", tmp_path / "key.json"
    main(["build", "--session", str(folder), "--bundle", str(bundle), "--key", str(key_path)])
    ratings_path = tmp_path / "ratings.json"
    e7.write(ratings_path, ratings_for(e7.read(key_path)))
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    capsys.readouterr()
    main(["score", "--ratings", str(ratings_path), "--key", str(key_path), "--manifest", str(folder / "manifest.json")])
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pass" and result["dry_run"]
    assert str(tmp_path) not in json.dumps(result)
    e7.write(key_path, {"tampered": True})
    with pytest.raises(ValueError, match="commitment"):
        scoring.score_files(ratings_path, key_path, folder / "manifest.json")


def test_child_turn_mini_stitches_and_missing_ids_fail(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("SOUL.md", "voice.md"):
        (inputs / name).write_text("fixture")
    monkeypatch.setattr(run, "key_value", lambda *a: "offline-fixture")
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(tts_tool=SimpleNamespace(text_to_speech_tool=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    closed = []
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", SimpleNamespace(_close_agent=lambda *a: closed.append(True)))
    class Response(BytesIO):
        headers = {"request-id": "fixture-id"}
    ctx = FakeContext({"backend": "eleven", "eleven_voice_id": "fixture", "eleven_seed": 42})
    provider = EmotionChipTTSProvider(Settings(ctx), data_dir=lambda: tmp_path,
        env=lambda k: "fixture-session" if k == "HERMES_SESSION_ID" else "",
        opener=lambda *a, **k: Response(b"fixture audio"))
    monkeypatch.setitem(sys.modules, "agent.tts_registry", SimpleNamespace(get_provider=lambda name: provider))
    count, histories = [0], []
    class Agent:
        session_id = "fixture-session"
        def run_conversation(self, prompt, **kwargs):
            assert kwargs == ({} if not histories else {"conversation_history": histories[-1]})
            count[0] += 1
            provider.synthesize("[soft: Hello.]", tmp_path / f"out/turn-{count[0]}.mp3")
            histories.append((histories[-1] if histories else []) + [{"role": "user", "content": prompt}])
            return {"completed": True, "messages": histories[-1]}
    driver = SimpleNamespace(build_agent=lambda cwd: (Agent(), None, {}), _recorder=turn._recorder,
                             wrap_upstream=lambda item: ["fake-relay"], tts_calls=lambda messages: ([], []),
                             stop_natives=lambda: None)
    args = {"mode": "e7-turn", "inputs": str(inputs), "key_file": "unread-fixture-reference",
            "item_dir": str(tmp_path), "cwd": str(tmp_path), "arm": "chip", "prompts": ["one", "two", "three"]}
    result = e7.child(args, driver)
    assert result["completed"] and closed == [True]
    assert [s["previous_request_ids"] for s in result["sent_to_voice"]] == [[], ["fixture-id"], ["fixture-id", "fixture-id"]]
    assert "ELEVENLABS_API_KEY" not in __import__("os").environ
    # Same child mode with no stitch identity must fail on the second turn.
    provider.env = lambda key: ""
    histories.clear()
    result = e7.child(args, driver)
    assert result["completed"] is False and result["error_type"] == "IntegrityError"


def test_child_comparison_render_seed_no_stitch_and_mismatch(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(run, "key_value", lambda *a: "offline-fixture")
    requests = []
    class Response(BytesIO):
        headers = {"request-id": "fixture"}
    from emotion_chip.hermes import eleven
    def opener(request, **k):
        requests.append(json.loads(request.data))
        return Response(b"rendered audio")
    monkeypatch.setattr(eleven, "urlopen", opener)
    args = {"mode": "e7-render", "inputs": str(tmp_path), "key_file": "unread-fixture-reference",
            "item_dir": str(tmp_path), "score": "[soft: Hello.]", "recorded": "[softly] Hello.",
            "voice": "fixture-ava", "seed": 123}
    result = e7.child(args, SimpleNamespace())
    assert len(result["clips"]) == 2 and result["cued_differs"]
    assert [r["text"] for r in requests] == ["[softly] Hello.", "Hello."]
    assert all(r["seed"] == 123 and r["voice_settings"] == e7.VOICE_SETTINGS and "previous_request_ids" not in r for r in requests)
    result = e7.child(dict(args, recorded="mismatch"), SimpleNamespace())
    assert result["completed"] is False and len(requests) == 2


def test_loudness_actual_high_crest_tone_lowers_global_target(tmp_path, generated):
    hot = tmp_path / "hot.wav"
    expression = r"0.003*sin(2*PI*1000*t)+if(lt(mod(t\,0.2)\,0.002)\,0.9*sin(2*PI*1000*t)\,0)"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc={expression}:s=44100:d=2",
                    str(hot)], capture_output=True, check=True)
    quiet = next(generated[2].rglob("*.mp3"))
    measurements = [e7.measure(quiet), e7.measure(hot)]
    target, gains = e7.gains(measurements)
    assert target < -18
    assert all(m["peak"] + gain <= -1 + 1e-9 for m, gain in zip(measurements, gains))
    assert gains[0] < -18 - measurements[0]["lufs"]


def test_generate_unvoiced_and_error_are_recorded(tmp_path, monkeypatch):
    paths = fixture_paths(tmp_path / "source")
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("a01", "chip", ["fixture"])]))
    monkeypatch.setattr(run, "archive", lambda repo, ref, dest: dest.mkdir(parents=True))
    folder = tmp_path / "unvoiced"
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    manifest = e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    assert manifest["cells"]["a01:chip"][0]["status"] == "unvoiced"
    monkeypatch.setattr(e7, "dry_cell", lambda *a: (_ for _ in ()).throw(OSError("offline fixture outage")))
    folder = tmp_path / "failure"
    e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    manifest = e7.read(folder / "manifest.json")
    assert manifest["cells"]["a01:chip"][0]["status"] == "error"
    (folder / "src/emotion-chip/changed").write_text("changed")
    with pytest.raises(ValueError, match="plugin tree"):
        e7.generate("fixture-tag", folder, dry_run=True, paths=paths)


def test_rule6_xing_encoder_only_and_extra_metadata(tmp_path):
    frame = bytearray(417)
    frame[:4] = b"\xff\xfb\x90\xc0"
    frame[21:29] = b"Xing" + bytes(4)
    frame[29:38] = b"LAME3.100"
    path = tmp_path / "header.mp3"
    path.write_bytes(frame)
    e7.check_mp3(path)
    frame[38] = 1
    path.write_bytes(frame)
    with pytest.raises(ValueError, match="extra metadata"):
        e7.check_mp3(path)


def test_rule10_both_comparisons_and_all_auxiliary_ratings_required(built, monkeypatch):
    _, _, folder, _, _, key = built
    manifest = e7.read(folder / "manifest.json")
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    ratings = ratings_for(key, wins=20)
    for trial in [t for t in key["trials"] if t["comparison"] == "comparison_b"][14:]:
        ratings["docs"]["lock"]["ratings"][trial["id"]]["choice"] = "none"
    result = scoring.score(ratings, key, manifest)
    assert result["status"] == "fail"
    assert result["comparisons"]["comparison_a"]["chip_preferred"] == 20
    assert result["comparisons"]["comparison_b"]["chip_preferred"] == 14
    for kind in ("catch", "mini"):
        ratings = ratings_for(key)
        trial = next(t for t in key["trials"] if t["kind"] == kind)
        del ratings["docs"]["lock"]["ratings"][trial["id"]]
        assert scoring.score(ratings, key, manifest)["status"] == "incomplete"
    ratings = ratings_for(key)
    trial = next(t for t in key["trials"] if t["comparison"] == "comparison_b")
    ratings["docs"]["lock"]["ratings"][trial["id"]]["acting"]["a"] = True
    result = scoring.score(ratings, key, manifest)
    assert result["comparisons"]["comparison_b"]["acting"][trial["sides"]["a"]] == 1
    assert result["notes"] == 47


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A single-cell generation, with fake archive and host access."""
    paths = fixture_paths(tmp_path / "source")
    folder = tmp_path / "session"
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("b01", "chip", ["fixture"])]))
    monkeypatch.setattr(run, "archive", lambda repo, ref, dest: dest.mkdir(parents=True))
    def generate(**kwargs):
        return e7.generate("fixture-tag", folder, dry_run=True, paths=paths, **kwargs)
    return folder, generate


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("bad", ["garbage", "truncated", "silent", "nonfinite"])
def test_fix4_undecodable_clip_is_retryable_and_good_clips_complete(ledger, monkeypatch, dry_run, bad):
    folder, dry_generate = ledger
    paths = fixture_paths(folder.parent / "real-source")
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("a01", "chip", ["fixture"])]))
    monkeypatch.setattr(e7, "check_credentials", lambda *a: None)
    def prepare(session, *a, **k):
        (session / "src/emotion-chip").mkdir(parents=True)
        return {"emotion_chip": "fixture-commit"}
    monkeypatch.setattr(run, "prepare", prepare)
    good = folder.parent / "good.mp3"
    e7.tone(good)
    returned = []
    def cell(session, *a):
        cued, stripped = session / "cued.mp3", session / "stripped.mp3"
        shutil.copyfile(good, cued)
        if bad == "garbage":
            stripped.write_bytes(b"not an MP3")
        elif bad == "truncated":
            stripped.write_bytes(good.read_bytes()[:32])
        elif bad == "silent":
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                            "-t", "1", str(stripped)], capture_output=True, check=True)
        else:
            shutil.copyfile(good, stripped)
        returned.extend([cued, stripped])
        return {"status": "completed", "clips": [cued.name, stripped.name]}
    monkeypatch.setattr(e7, "dry_cell" if dry_run else "real_cell", cell)
    original_measure = e7.measure
    measured = []
    def measure(clip):
        measured.append(Path(clip).name)
        if bad == "nonfinite" and Path(clip).name == "stripped.mp3":
            return {"lufs": float("nan"), "peak": -1.0, "command": []}
        return original_measure(clip)
    monkeypatch.setattr(e7, "measure", measure)
    def generate(**kwargs):
        return dry_generate(**kwargs) if dry_run else e7.generate("fixture-tag", folder, paths=paths, **kwargs)
    manifest = generate()
    attempt = manifest["cells"]["a01:chip"][0]
    assert attempt["status"] == "synthesis_failed"
    assert attempt["reason"] == "clip did not decode"
    assert attempt["clips"] == []
    assert attempt["rejected_clips"] == [p.name for p in returned]
    assert attempt["rejected_clip_sha256"] == [e7.sha(p) for p in returned]
    assert all(p.exists() for p in returned)
    assert measured == ["cued.mp3", "stripped.mp3"]
    monkeypatch.setattr(e7, "dry_cell" if dry_run else "real_cell",
                        lambda *a: {"status": "completed", "clips": ["cued.mp3"]})
    manifest = generate(retry="a01", reason="decoder failure resolved")
    assert [a["status"] for a in manifest["cells"]["a01:chip"]] == ["synthesis_failed", "completed"]
    assert manifest["cells"]["a01:chip"][0] == attempt
    assert manifest["attempt_counts"]["chip"]["synthesis_failed"] == 1
    assert manifest["attempt_counts"]["chip"]["completed"] == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_fix4_interrupted_preparation_is_archived_and_redone(ledger, monkeypatch, dry_run):
    folder, dry_generate = ledger
    paths = fixture_paths(folder.parent / "real-source")
    def archive(repo, ref, dest):
        dest.mkdir(parents=True)
        (dest / "complete.txt").write_text(ref)
    monkeypatch.setattr(run, "archive", archive)
    clean = folder.parent / "clean-archive"
    archive(paths["chip_repo"], "fixture-commit", clean)
    (folder / "src/emotion-chip").mkdir(parents=True)
    (folder / "src/emotion-chip/partial.txt").write_text("interrupted copy")
    for name in ("cwd", "tools", "golden"):
        (folder / name).mkdir()
        (folder / name / "partial.txt").write_text("keep")
    for name in ("sandbox.sb", "secrets.env"):
        (folder / name).write_text("offline fixture")
    (folder / "interrupted-prepare-1").mkdir()
    (folder / "interrupted-prepare-1/keep.txt").write_text("older interruption")
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([]))
    monkeypatch.setattr(e7, "check_credentials", lambda *a: None)
    def prepare(session, *a, **k):
        assert not any((session / name).exists() for name in ("src", "cwd", "tools", "golden", "sandbox.sb", "secrets.env"))
        archive(paths["chip_repo"], "fixture-commit", session / "src/emotion-chip")
        return {"emotion_chip": "fixture-commit"}
    monkeypatch.setattr(run, "prepare", prepare)
    def generate():
        return dry_generate() if dry_run else e7.generate("fixture-tag", folder, paths=paths)
    manifest = generate()
    assert manifest["plugin_tree"] == run.tree_sha256(clean)
    assert manifest["shas"] == ({} if dry_run else {"emotion_chip": "fixture-commit"})
    assert e7.read(folder / "prepared.json") == manifest["shas"]
    moved = folder / "interrupted-prepare-2"
    assert (moved / "src/emotion-chip/partial.txt").read_text() == "interrupted copy"
    assert {p.name for p in moved.iterdir()} == {"src", "cwd", "tools", "golden", "sandbox.sb", "secrets.env"}
    assert (folder / "interrupted-prepare-1/keep.txt").read_text() == "older interruption"
    # The receipt, rather than the src directory, admits reuse and restores its shas.
    manifest["shas"] = {"wrong": "not the prepared receipt"}
    e7.write(folder / "manifest.json", manifest)
    monkeypatch.setattr(run, "prepare", lambda *a, **k: pytest.fail("prepared tree rebuilt"))
    monkeypatch.setattr(run, "archive", lambda *a: pytest.fail("prepared tree re-archived"))
    assert generate()["shas"] == e7.read(folder / "prepared.json")


@pytest.mark.parametrize("exception", [ValueError, RuntimeError, OSError, KeyboardInterrupt, SystemExit])
def test_fix1_every_cell_exception_is_finalized(ledger, monkeypatch, exception):
    folder, generate = ledger
    def fail(*a):
        raise exception("private text and credential-shaped payload must not be copied")
    monkeypatch.setattr(e7, "dry_cell", fail)
    if issubclass(exception, Exception):
        generate()
    else:
        with pytest.raises(exception):
            generate()
    manifest = e7.read(folder / "manifest.json")
    attempt = manifest["cells"]["b01:chip"][0]
    assert attempt["status"] == "error" and attempt["clips"] == []
    assert attempt["reason"].startswith(exception.__name__ + ": ")
    assert "private text" not in json.dumps(manifest)
    assert manifest["attempt_counts"]["chip"]["error"] == 1


def test_fix1_clip_hash_failure_is_finalized(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "completed", "clips": ["missing.mp3"]})
    generate()
    row = e7.read(folder / "manifest.json")["cells"]["b01:chip"][0]
    assert row["status"] == "error" and row["clip_sha256"] == []


def test_fix1_killed_attempt_becomes_retryable_error(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    generate()
    manifest = e7.read(folder / "manifest.json")
    manifest["cells"]["b01:chip"] = [{"status": "started", "clips": []}]
    e7.write(folder / "manifest.json", manifest)
    monkeypatch.setattr(e7, "dry_cell", lambda *a: pytest.fail("unrequested regeneration"))
    generate()
    manifest = e7.read(folder / "manifest.json")
    assert manifest["cells"]["b01:chip"][0]["status"] == "error"
    assert manifest["attempt_counts"]["chip"]["error"] == 1
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    assert len(generate(retry="b01", reason="interruption resolved")["cells"]["b01:chip"]) == 2


@pytest.mark.parametrize("reason", e7.FAILURE_REASONS)
def test_fix1_retry_by_reason_is_bounded_and_keeps_all_attempts(ledger, monkeypatch, reason):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": reason, "reason": reason, "clips": []})
    if reason == "credential":
        with pytest.raises(RuntimeError, match="credential"):
            generate()
        with pytest.raises(ValueError, match="credential"):
            generate(retry="b01", reason="try again")
        return
    generate()
    with pytest.raises(ValueError, match="requires --reason"):
        generate(retry="b01")
    for _ in range(2):
        generate(retry="b01", reason="operator checked the cause")
    with pytest.raises(ValueError, match="at most 2 retries"):
        generate(retry="b01", reason="fourth attempt")
    manifest = e7.read(folder / "manifest.json")
    assert len(manifest["cells"]["b01:chip"]) == 3
    assert manifest["attempt_counts"]["chip"][reason] == 3
    assert manifest["attempt_counts"]["eva"][reason] == 0


def test_fix1_credential_stops_before_next_cell(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("b01", "chip", ["one"]), ("b02", "eva", ["two"])]))
    seen = []
    def cell(session, item, *args):
        seen.append(item)
        return {"status": "credential", "clips": []}
    monkeypatch.setattr(e7, "dry_cell", cell)
    with pytest.raises(RuntimeError, match="credential"):
        generate()
    assert seen == ["b01"]
    assert "b02:eva" not in e7.read(folder / "manifest.json")["cells"]


def test_fix1_first_usable_attempt_counts_and_is_never_replaced(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "synthesis_failed", "clips": []})
    generate()
    e7.tone(folder / "fixture.mp3")
    original = (folder / "fixture.mp3").read_bytes()
    good = {"status": "completed", "clips": ["fixture.mp3"]}
    monkeypatch.setattr(e7, "dry_cell", lambda *a: good)
    manifest = generate(retry="b01", reason="synthesis recovered")
    row, clips = e7.completed(manifest, "b01", "chip", folder)
    assert row["status"] == "completed" and clips[0].read_bytes() == original
    monkeypatch.setattr(e7, "dry_cell", lambda *a: pytest.fail("usable attempt replaced"))
    generate()
    with pytest.raises(ValueError, match="recorded failure"):
        generate(retry="b01", reason="cherry pick")
    assert len(e7.read(folder / "manifest.json")["cells"]["b01:chip"]) == 2


def test_fix1_retry_budget_is_shared_between_arms(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("b01", "chip", ["one"]), ("b01", "eva", ["one"])]))
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    generate()
    generate(retry="b01", reason="first retry")
    generate(retry="b01", reason="second retry")
    with pytest.raises(ValueError, match="at most 2"):
        generate(retry="b01", reason="third retry")
    assert {len(a) for a in e7.read(folder / "manifest.json")["cells"].values()} == {3}


@pytest.fixture
def real_case(tmp_path, monkeypatch):
    """Exercise real_cell, faking only the host and private-input setup."""
    from bench import gen
    folder = tmp_path / "session"
    calls, joined = [], []
    serial = [0]
    monkeypatch.setattr(gen, "keychain_token", lambda: "offline-fixture")
    def build_home(item, *a):
        (item / "out").mkdir(parents=True)
        return "frozen-soul"
    monkeypatch.setattr(e7, "build_home", build_home)
    monkeypatch.setattr(run, "read_jsonl", lambda p: [{
        "event": "upstream" if p.name == "upstream.jsonl" else "pre_api_request",
        "model": run.MODEL, "canaries": dict.fromkeys((*run.FIXED_CANARIES, "fixture"), False)}])
    def concat(sources, dest):
        joined.append([p.read_bytes() for p in sources])
        dest.write_bytes(b"|".join(p.read_bytes() for p in sources))
        return True
    monkeypatch.setattr(run, "concat_clips", concat)
    def invoke(proof, *, item="b01", arm="chip", messages=None, warm=None, rendered=None):
        serial[0] += 1
        class Host:
            def __init__(self, *a):
                pass
            def call(self, cell, args, **kwargs):
                calls.append(args)
                if args["mode"] == "e7-warm":
                    return {"soul_sha256": "frozen-soul"} if warm is None else warm
                if args["mode"] == "e7-render":
                    if rendered is not None:
                        return rendered
                    clips = [cell / "out/cued.mp3", cell / "out/stripped.mp3"]
                    for clip in clips:
                        clip.write_bytes(clip.stem.encode())
                    return {"clips": [str(p) for p in clips], "cued_differs": True}
                rows = proof.get("sent_to_voice", []) + [r for t in proof.get("turns", []) for r in t.get("sent_to_voice", [])]
                for row in rows:
                    if row.get("clip") and not row.get("missing_file"):
                        (cell / "out" / row["clip"]).write_bytes(b"" if row.get("empty_file") else row["clip"].encode())
                return proof
        monkeypatch.setattr(e7, "E7Host", Host)
        return e7.real_cell(folder, item, arm, messages or ["fixture"], 42,
                            {"ava_voice_id": "fixture", "canaries": ["fixture"]}, serial[0])
    return folder, invoke, calls, joined


@pytest.mark.parametrize("proof,warm,status", [
    ({}, {"error": "relay timeout"}, "turn_error"),
    ({}, {"rc": 1}, "turn_error"),
    ({}, {"credential_failure": True}, "credential"),
    ({}, {"error": "HTTP 401"}, "credential"),
    ({}, {"soul_sha256": "changed"}, "integrity"),
    ({"rc": 1}, None, "turn_error"),
    ({"completed": False, "error_type": "TimeoutError"}, None, "turn_error"),
    ({"completed": True, "failed": True}, None, "turn_error"),
    ({"completed": True, "error": "overload"}, None, "turn_error"),
    ({"credential_failure": True}, None, "credential"),
    ({"final_response": "access token has been revoked"}, None, "credential"),
    ({"completed": True, "tts_results": [{"error": "HTTP 401"}]}, None, "credential"),
    ({"completed": True, "turns": [{"tts_results": [{"error": "HTTP 401"}]}]}, None, "credential"),
    ({"completed": False, "integrity": True}, None, "integrity"),
    ({"completed": True, "sent_to_voice": [], "tts_calls": []}, None, "unvoiced"),
    ({"completed": True, "sent_to_voice": [], "tts_calls": [{"text": "fixture"}]}, None, "synthesis_failed"),
    ({"completed": True, "sent_to_voice": [{"text": "failed"}]}, None, "synthesis_failed"),
])
def test_fix1_real_cell_status_mapping(real_case, proof, warm, status):
    _, invoke, _, _ = real_case
    result = invoke(proof, warm=warm)
    assert result["status"] == result["reason"] == status
    assert result["clips"] == []


@pytest.mark.parametrize("arm", ["chip", "eva"])
@pytest.mark.parametrize("successes", [1, 2])
def test_fix1_real_cell_selects_successes_in_order(real_case, arm, successes):
    folder, invoke, _, joined = real_case
    rows = [{"text": "failure"}] + [{"clip": f"good-{i}.mp3"} for i in range(successes)] + [{"text": "failure"}]
    result = invoke({"completed": True, "sent_to_voice": rows}, arm=arm)
    assert result["status"] == "completed" and len(result["clips"]) == 1
    assert (folder / result["clips"][0]).read_bytes() == b"|".join(f"good-{i}.mp3".encode() for i in range(successes))
    assert len(joined) == (successes > 1)


@pytest.mark.parametrize("successes", [1, 2])
def test_fix1_comparison_a_requires_one_success_and_uses_its_score(real_case, successes):
    _, invoke, calls, _ = real_case
    rows = [{"text": "failed", "score": "wrong score"}] + [
        {"clip": f"good-{i}.mp3", "text": "recorded", "score": "successful score"} for i in range(successes)]
    result = invoke({"completed": True, "sent_to_voice": rows, "scores": ["wrong score", "successful score"]}, item="a01")
    assert result["status"] == ("completed" if successes == 1 else "integrity")
    renders = [c for c in calls if c["mode"] == "e7-render"]
    if successes == 1:
        assert renders[0]["score"] == "successful score" and renders[0]["recorded"] == "recorded"
    else:
        assert renders == []


@pytest.mark.parametrize("rendered,status", [
    ({"completed": False, "integrity": True}, "integrity"),
    ({"credential_failure": True}, "credential"),
    ({"clips": []}, "synthesis_failed"),
])
def test_fix1_comparison_a_render_failure_reason(real_case, rendered, status):
    _, invoke, _, _ = real_case
    result = invoke({"completed": True, "sent_to_voice": [{"clip": "good.mp3", "text": "text", "score": "score"}]},
                    item="a01", rendered=rendered)
    assert result["status"] == status and not result["clips"]


@pytest.mark.parametrize("missing", [False, True])
def test_fix1_mini_session_concatenates_per_turn_and_checks_stitching(real_case, missing):
    folder, invoke, _, joined = real_case
    turns = [{"sent_to_voice": [{"text": "failed"}] + [
        {"clip": f"turn-{i}-{j}.mp3", "previous_request_ids": [] if missing or i == 0 else ["earlier"]}
        for j in range(2)]} for i in range(3)]
    result = invoke({"completed": True, "turns": turns}, item="m1", messages=["one", "two", "three"])
    assert result["status"] == ("integrity" if missing else "completed")
    if not missing:
        assert len(result["clips"]) == len(joined) == 3
        for i, clip in enumerate(result["clips"]):
            assert (folder / clip).read_bytes() == f"turn-{i}-0.mp3|turn-{i}-1.mp3".encode()


@pytest.mark.parametrize("failure", ["raise", "false", "error_object"])
def test_fix1_eva_capture_ignores_failed_command_runs(tmp_path, monkeypatch, failure):
    (tmp_path / "out").mkdir()
    output = tmp_path / "out/stale.mp3"
    output.write_bytes(b"partial file")
    def command(*a, **k):
        if failure == "raise":
            raise RuntimeError("failed command")
        return False if failure == "false" else {"error": "failed"}
    module = SimpleNamespace(_generate_command_tts=command, text_to_speech_tool=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(tts_tool=module))
    sent, _, _ = e7.install_capture({"arm": "eva", "item_dir": str(tmp_path)}, turn)
    if failure == "raise":
        with pytest.raises(RuntimeError):
            module._generate_command_tts("text", output)
    else:
        module._generate_command_tts("text", output)
    assert sent == []


def test_fix1_eva_command_provider_voice_result_compatibility(tmp_path):
    cfg = e7.config(tmp_path, "eva", tmp_path, {"ava_voice_id": "fixture"}, 42)
    assert cfg["tts"]["providers"]["e7-sag"]["voice_compatible"] is True


@pytest.mark.parametrize("check", ["hash", "soul", "voice", "canary"])
def test_fix1_each_warm_guard_independently(tmp_path, monkeypatch, check):
    home, inputs = tmp_path / "home", tmp_path / "inputs"
    home.mkdir(); inputs.mkdir()
    (home / "SOUL.md").write_text("frozen persona")
    (inputs / "voice.md").write_text("separate frozen voice")
    monkeypatch.setenv("HERMES_HOME", str(home))
    prompt = "frozen persona\nseparate frozen voice"
    args = {"cwd": str(tmp_path), "arm": "eva", "soul_sha256": e7.sha(home / "SOUL.md"), "inputs": str(inputs)}
    if check == "hash":
        args["soul_sha256"] = "wrong digest"
    elif check == "soul":
        prompt = "separate frozen voice"
    elif check == "voice":
        prompt = "frozen persona"
    closed = []
    agent = SimpleNamespace(ephemeral_system_prompt="", close=lambda: closed.append("agent"))
    db = SimpleNamespace(close=lambda: closed.append("db"))
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "agent.system_prompt", SimpleNamespace(build_system_prompt=lambda agent: prompt))
    driver = SimpleNamespace(build_agent=lambda cwd: (agent, db, {}), canaries=lambda text: {"private": check == "canary"})
    with pytest.raises(RuntimeError, match={"hash": "SOUL", "soul": "SOUL", "voice": "voice", "canary": "canary"}[check]):
        e7.warm_proof(args, driver)
    assert closed == ["agent", "db"]


@pytest.mark.parametrize("mode", ["e7-warm", "e7-turn", "e7-render"])
def test_fix1_turn_main_dispatches_archived_e7_child(tmp_path, monkeypatch, capsys, mode):
    from contextlib import contextmanager
    args = {"mode": mode, "chip_root": str(tmp_path / "archived-chip")}
    path = tmp_path / "args.json"
    e7.write(path, args)
    events = []
    @contextmanager
    def scope(profile):
        assert profile is None
        events.append("enter")
        yield
        events.append("exit")
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", SimpleNamespace(discover_plugins=lambda: events.append("discover")))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", SimpleNamespace(_config_profile_scope=scope))
    def child(received, driver):
        assert received == args and driver is turn
        assert sys.path[0] == args["chip_root"]
        events.append("child")
        return {"offline": True}
    monkeypatch.setattr(e7, "child", child)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", [str(turn.__file__), str(path)])
    def exit(code):
        assert code == 0
        raise SystemExit(code)
    monkeypatch.setattr(turn.os, "_exit", exit)
    with pytest.raises(SystemExit):
        turn.main()
    assert events == ["enter", "discover", "child", "exit"]
    assert capsys.readouterr().out.strip() == 'PROOF={"offline": true}'


def frozen_repo(path, message="frozen"):
    """Write a tiny fixture repository directly; every git command in this lane stays read-only."""
    import hashlib
    import struct
    import zlib
    meta = path / ".git"
    (meta / "objects").mkdir(parents=True)
    (meta / "refs/tags").mkdir(parents=True)
    (meta / "refs/heads").mkdir()
    (meta / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")
    def object(kind, content):
        raw = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
        digest = hashlib.sha1(raw).hexdigest()
        dest = meta / "objects" / digest[:2] / digest[2:]
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(zlib.compress(raw))
        return digest
    content = b"frozen\n"
    blob = object("blob", content)
    name = b"tracked.txt"
    tree = object("tree", b"100644 " + name + b"\0" + bytes.fromhex(blob))
    commit = object("commit", f"tree {tree}\nauthor Fixture <fixture@example.invalid> 1 +0000\ncommitter Fixture <fixture@example.invalid> 1 +0000\n\n{message}\n".encode())
    (meta / "HEAD").write_text(commit + "\n")
    (meta / "refs/tags/frozen").write_text(commit + "\n")
    (meta / "refs/heads/branch").write_text(commit + "\n")
    (path / name.decode()).write_bytes(content)
    entry = struct.pack(">10I20sH", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, len(content), bytes.fromhex(blob), len(name)) + name + b"\0"
    entry += b"\0" * (-len(entry) % 8)
    index = struct.pack(">4sII", b"DIRC", 2, 1) + entry
    (meta / "index").write_bytes(index + hashlib.sha1(index).digest())
    return commit


@pytest.mark.parametrize("dirty_repo", ["chip", "bench"])
def test_fix1_frozen_tag_checks_two_repos_and_ignores_untracked(tmp_path, monkeypatch, dirty_repo):
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    repos = {name: tmp_path / name for name in ("chip", "bench")}
    commits = {name: frozen_repo(path) for name, path in repos.items()}
    assert commits["chip"] == commits["bench"]
    (repos["bench"] / "untracked.txt").write_text("not a tracked edit")
    assert e7.frozen_tag("frozen", repos["chip"], repos["bench"]) == commits["chip"]
    with pytest.raises(ValueError, match="branches"):
        e7.frozen_tag("branch", repos["chip"], repos["bench"])
    (repos[dirty_repo] / "tracked.txt").write_text("changed")
    with pytest.raises(ValueError, match="dirty"):
        e7.frozen_tag("frozen", repos["chip"], repos["bench"])


@pytest.mark.parametrize("changed_repo", ["chip", "bench"])
def test_fix1_frozen_tag_checks_both_heads(tmp_path, monkeypatch, changed_repo):
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    repos = {name: tmp_path / name for name in ("chip", "bench")}
    commits = {name: frozen_repo(path) for name, path in repos.items()}
    different = tmp_path / "different"
    other_commit = frozen_repo(different, "other commit")
    shutil.copytree(different / ".git/objects", repos[changed_repo] / ".git/objects", dirs_exist_ok=True)
    (repos[changed_repo] / ".git/HEAD").write_text(other_commit + "\n")
    with pytest.raises(ValueError, match="HEAD"):
        e7.frozen_tag("frozen", repos["chip"], repos["bench"])


@pytest.mark.parametrize("status", [None, "pass", "fail", "incomplete", "void"])
def test_fix1_rerun_requires_void_score_receipt_bound_to_key(built, tmp_path, monkeypatch, status):
    _, _, source, _, source_key, key = built
    parent = tmp_path / "first"
    parent.mkdir()
    key_path = tmp_path / "key.json"
    shutil.copyfile(source_key, key_path)
    manifest = e7.read(source / "manifest.json")
    manifest.update(key_path=str(key_path), locked_at="2026-10-22T12:00:00Z",
                    ratings_sha256="fixture-ratings", score={"status": "void"})
    e7.write(parent / "manifest.json", manifest)
    if status is not None:
        e7.write(e7.score_receipt_path(key_path), {"status": status, "key_sha256": e7.sha(key_path),
                                                "ratings_sha256": manifest["ratings_sha256"]})
    now = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
    if status != "void":
        monkeypatch.setattr(e7, "load_paths", lambda: pytest.fail("rerun got beyond receipt gate"))
        with pytest.raises(ValueError, match="void score receipt"):
            e7.build(tmp_path / "new", tmp_path / "bundle", tmp_path / "new-key", rerun_of=parent, now=now)
        assert not (parent / "rerun.json").exists()
    else:
        assert e7.rerun_admission(parent, tmp_path / "new", now)["locked_at"] == manifest["locked_at"]
        e7.write(e7.score_receipt_path(key_path), {"status": "void", "key_sha256": "wrong",
                                                "ratings_sha256": manifest["ratings_sha256"]})
        with pytest.raises(ValueError, match="commitment"):
            e7.rerun_admission(parent, tmp_path / "new", now)
        e7.write(e7.score_receipt_path(key_path), {"status": "void", "key_sha256": manifest["key_sha256"],
                                                "ratings_sha256": manifest["ratings_sha256"]})
        key_path.write_text("tampered")
        with pytest.raises(ValueError, match="commitment"):
            e7.rerun_admission(parent, tmp_path / "new", now)


def test_fix1_score_writes_key_bound_receipt_and_attempt_counts(built, tmp_path, monkeypatch):
    _, _, source, _, source_key, key = built
    manifest_path, key_path, ratings_path = (tmp_path / name for name in ("manifest.json", "key.json", "ratings.json"))
    shutil.copyfile(source_key, key_path)
    manifest = e7.read(source / "manifest.json")
    manifest["cells"]["b01:chip"].insert(0, {"status": "turn_error", "clips": []})
    manifest["cells"]["b01:eva"].insert(0, {"status": "synthesis_failed", "clips": []})
    e7.write(manifest_path, manifest)
    e7.write(ratings_path, ratings_for(key))
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    result = scoring.score_files(ratings_path, key_path, manifest_path)
    assert result["attempt_counts"]["chip"]["turn_error"] == 1
    assert result["attempt_counts"]["eva"]["synthesis_failed"] == 1
    assert e7.read(e7.score_receipt_path(key_path)) == {"status": "pass", "key_sha256": e7.sha(key_path),
                                                     "ratings_sha256": e7.sha(ratings_path)}
    # A fresh, void score has no comparison numbers, including in its adjacent receipt.
    e7.write(manifest_path, manifest)
    e7.write(ratings_path, ratings_for(key, catches=2))
    assert scoring.score_files(ratings_path, key_path, manifest_path) == {"status": "void", "catch_preferences": 2}
    assert e7.read(e7.score_receipt_path(key_path)) == {"status": "void", "key_sha256": e7.sha(key_path),
                                                     "ratings_sha256": e7.sha(ratings_path)}


def test_fix1_bundle_publishes_only_shared_user_messages(built):
    _, _, _, bundle, _, key = built
    page = (bundle / "index.html").read_text()
    import re
    trials = json.loads(re.search(r"const TRIALS = (.*);", page)[1])
    private = {t["id"]: t for t in key["trials"]}
    data = e7.prompts()
    for trial in trials:
        row = private[trial["id"]]
        if row["kind"] == "mini":
            assert trial["messages"] == data["mini_sessions"][row["item"]]
            assert len(trial["messages"]) == len(trial["clips"]) == 3
        else:
            assert trial["message"] == data[row["comparison"] or "catch"][row["item"]]
        assert not ({"item", "comparison", "seed", "sides"} & trial.keys())


PAGE_DOM = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.style = {};
    this.listeners = {}; this.disabled = false; this.value = ''; this.textContent = ''; this.className = '';
  }
  append(...children) { this.children.push(...children.filter(c => typeof c !== 'string')); }
  insertBefore(child, before) { this.children.splice(this.children.indexOf(before), 0, child); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  pause() {}
  set innerHTML(value) {
    if (this.tagName === 'ARTICLE') {
      const title = new Element('span'), players = new Element('div');
      title.className = 'tid'; players.className = 'players'; this.append(title, players);
    }
  }
  querySelectorAll(selector) {
    const matches = el => selector.split(',').some(s =>
      s[0] === '.' ? el.className.split(' ').includes(s.slice(1)) :
      s[0] === '#' ? el.id === s.slice(1) :
      s === '[data-v]' ? 'v' in el.dataset : s === '[data-side]' ? 'side' in el.dataset : el.tagName === s.toUpperCase());
    return this.children.flatMap(el => [...(matches(el) ? [el] : []), ...el.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const root = new Element('body');
for (const id of ['trials', 'lock', 'status', 'count', 'fill']) {
  const el = new Element(id === 'lock' ? 'button' : 'div'); el.id = id; root.append(el);
}
const document = {createElement: tag => new Element(tag), activeElement: null,
  querySelector: s => root.querySelector(s), querySelectorAll: s => root.querySelectorAll(s),
  getElementById: id => root.querySelector('#' + id)};
let clock = 0, timerId = 0;
const timers = new Map();
const setTimeout = (callback, delay) => {timers.set(++timerId, {callback, due: clock + delay}); return timerId;};
const clearTimeout = id => timers.delete(id);
function tick(ms) {
  clock += ms;
  for (const [id, timer] of [...timers]) if (timer.due <= clock) {timers.delete(id); timer.callback();}
}
async function drain() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
let confirmAnswer = true, confirmations = 0, listener, hold = false;
const writes = [], releases = [], stored = new Map();
const snapshot = (id, data) => ({id, exists: data !== undefined, data: () => structuredClone(data)});
const store = {
  doc: path => ({get: async () => snapshot(path.split('/').at(-1), stored.get(path.split('/').at(-1))),
    set: body => {
      writes.push({path, body: structuredClone(body)});
      const finish = () => stored.set(path.split('/').at(-1), structuredClone(body));
      if (hold && !path.endsWith('/lock')) return new Promise(resolve => releases.push(() => {finish(); resolve();}));
      finish(); return Promise.resolve();
    }}),
  collection: name => ({get: async () => ({docs: [...stored].map(([id, data]) => snapshot(id, data))}),
    onSnapshot: callback => {listener = callback;}})
};
const window = {confirm: () => {confirmations++; return confirmAnswer;}, claude: {use: async () => store}};
'''


def page_check(tmp_path, test):
    script = (e7.HERE / "template.html").read_text().split("<script>")[1].split("</script>")[0]
    script = script[:script.rindex("\nbuild();")]
    trials = [{"id": "t01", "a": "a.mp3", "b": "b.mp3", "message": "Shared <user> moment"},
              {"id": "m01", "clips": ["1.mp3", "2.mp3", "3.mp3"], "messages": ["First moment", "Second moment", "Third moment"]}]
    script = script.replace("/*TRIALS*/[]", json.dumps(trials))
    path = tmp_path / "page-test.js"
    path.write_text(PAGE_DOM + script + "\n(async () => {\n" + test + "\nconsole.log('page verified');\n})().catch(e => {console.error(e); process.exitCode = 1;});")
    result = subprocess.run([shutil.which("node"), str(path)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "page verified"


def test_fix1_page_moments_progress_and_confirmation(tmp_path):
    page_check(tmp_path, r'''
build(); await load();
const pair = document.getElementById('trial-t01'), mini = document.getElementById('trial-m01');
assert.equal(pair.querySelector('.prompt').textContent, 'Shared <user> moment');
assert.equal(pair.children.indexOf(pair.querySelector('.prompt')) + 1, pair.children.indexOf(pair.querySelector('.players')));
mini.querySelector('.players').children.forEach((player, i) => {
  assert.equal(player.children[0].textContent, TRIALS[1].messages[i]);
  assert.equal(player.children[2].tagName, 'AUDIO');
});
assert.equal(document.querySelector('#count').textContent, '0 of 2 rated');
assert.equal(document.querySelector('#lock').disabled, true);
await lockRatings(); assert.equal(confirmations, 0);
await save('t01', {choice:'none'});
assert.equal(document.querySelector('#count').textContent, '1 of 2 rated');
assert.equal(document.querySelector('#lock').disabled, true);
await save('m01', {rating:4});
assert.equal(document.querySelector('#count').textContent, '2 of 2 rated');
assert.equal(document.querySelector('#lock').disabled, false);
confirmAnswer = false;
await lockRatings(); assert.equal(confirmations, 1); assert.equal(locked, false);
assert.ok(!writes.some(w => w.path.endsWith('/lock')));
confirmAnswer = true;
await lockRatings(); assert.equal(confirmations, 2); assert.equal(locked, true);
assert.ok(document.querySelectorAll('button,input,textarea,select,audio').every(el => el.disabled));
''')


def test_fix4_page_lock_snapshot_includes_flushed_note_and_all_saved_docs(tmp_path):
    page_check(tmp_path, r'''
build(); await load();
const note = document.getElementById('trial-t01').querySelector('textarea');
assert.equal(note.maxLength, 2000);
note.value = 'first'; note.listeners.input();
tick(400); note.value = 'latest'; note.listeners.input();
tick(799); await drain(); assert.equal(writes.length, 0);
tick(1); await drain(); assert.equal(writes.length, 1); assert.equal(writes[0].body.note, 'latest');
await save('t01', {choice:'a'}); await save('m01', {rating:3});
note.value = 'final note'; note.listeners.input();
stored.set('other-saved-doc', {note: 'from another tab'});
let reads = 0;
const originalCollection = store.collection;
store.collection = name => ({...originalCollection(name), get: async () => {
  reads++; return originalCollection(name).get();
}});
await lockRatings();
const lastRating = writes.at(-2), lock = writes.at(-1);
assert.equal(lastRating.body.note, 'final note');
assert.ok(lock.path.endsWith('/lock')); assert.equal(timers.size, 0);
assert.equal(reads, 1);
assert.equal(writes.filter(w => w.path.endsWith('/lock')).length, 1);
assert.deepEqual(Object.keys(lock.body).sort(), ['lockedAt', 'ratings']);
assert.deepEqual(lock.body.ratings, Object.fromEntries([...stored].filter(([id]) => id !== 'lock')));
assert.equal(lock.body.ratings.t01.note, 'final note');
assert.equal(lock.body.ratings.m01.rating, 3);
stored.set('t01', {...stored.get('t01'), note: 'late change'});
assert.equal(lock.body.ratings.t01.note, 'final note');
tick(800); await drain(); assert.equal(writes.at(-1), lock);
''')


def test_fix1_page_lock_awaits_all_pending_saves(tmp_path):
    page_check(tmp_path, r'''
build(); await load(); hold = true;
const first = save('t01', {choice:'a'}), second = save('m01', {rating:5});
const lockingWork = lockRatings();
await drain(); assert.equal(writes.length, 1); assert.equal(locked, false);
releases.shift()(); await first; await drain();
assert.equal(writes.length, 2); assert.ok(!writes.some(w => w.path.endsWith('/lock')));
releases.shift()(); await second; await lockingWork;
assert.equal(writes.length, 3); assert.ok(writes[2].path.endsWith('/lock')); assert.equal(locked, true);
''')


def test_fix1_page_second_tab_observes_any_lock_and_stops_saving(tmp_path):
    page_check(tmp_path, r'''
build(); await load();
save('t01', {note:'pending note'}, true);
listener({docs:[snapshot('lock', {})]});
assert.equal(locked, true);
assert.ok(document.querySelectorAll('button,input,textarea,select,audio').every(el => el.disabled));
await save('t01', {choice:'b'}); tick(800); await drain();
assert.equal(writes.length, 0); assert.equal(state.t01.choice, null);
''')


def test_fix1_page_lock_rechecks_store_before_writing(tmp_path):
    page_check(tmp_path, r'''
build(); await load();
await save('t01', {choice:'b'}); await save('m01', {rating:2});
const before = writes.length;
stored.set('lock', {lockedAt:'earlier tab'});
await lockRatings();
assert.equal(locked, true); assert.equal(writes.length, before);
''')


def test_fix1_page_load_respects_existing_lock(tmp_path):
    page_check(tmp_path, r'''
stored.set('lock', {});
build(); await load();
assert.equal(locked, true); await save('t01', {choice:'a'});
assert.equal(writes.length, 0);
assert.ok(document.querySelectorAll('button,input,textarea,select,audio').every(el => el.disabled));
''')


@pytest.mark.parametrize("result,credential", [
    ({"completed": False, "error": "overloaded"}, False),
    ({"completed": True, "failed": True}, False),
    ({"completed": True, "final_response": "HTTP 401"}, True),
    *[({"completed": True, "final_response": f"Let me explain {word}."}, False)
      for word in ("401", "unauthorized", "unauthorised", "missing API key", "quota_exceeded")],
    ({"completed": True, "messages": [{"role": "tool", "name": "text_to_speech", "content": '{"error":"HTTP 401"}'}]}, True),
])
def test_fix3_child_preserves_turn_and_credential_failure_proof(tmp_path, monkeypatch, result, credential):
    monkeypatch.setattr(run, "key_value", lambda *a: "offline-fixture")
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", SimpleNamespace(_close_agent=lambda *a: None))
    monkeypatch.setattr(e7, "install_capture", lambda *a: ([], [], []))
    driver = SimpleNamespace(build_agent=lambda cwd: (SimpleNamespace(run_conversation=lambda *a, **k: result), None, {}),
        wrap_upstream=lambda item: [], tts_calls=turn.tts_calls, stop_natives=lambda: None)
    args = {"mode": "e7-turn", "inputs": str(tmp_path), "key_file": "unread-fixture-reference", "item_dir": str(tmp_path),
            "cwd": str(tmp_path), "arm": "chip", "prompts": ["fixture"]}
    proof = e7.child(args, driver)
    assert proof["credential_failure"] is credential
    if not credential:
        assert proof["completed"] is bool(result.get("completed") and not result.get("failed") and not result.get("error"))


def test_fix1_cli_forwards_reason_based_retry(monkeypatch, capsys):
    from bench.e7 import __main__ as cli
    calls = []
    monkeypatch.setattr(cli, "generate", lambda *a, **k: calls.append((a, k)))
    cli.main(["generate", "--tag", "fixture-tag", "--session", "fixture-session", "--dry-run",
              "--retry", "b01", "--reason", "cause resolved"])
    assert calls == [(("fixture-tag", "fixture-session"),
                      {"dry_run": True, "retry": "b01", "reason": "cause resolved"})]
    assert capsys.readouterr().out.strip() == "generation recorded"


@pytest.mark.parametrize("unavailable", [None, "claude", "eleven"])
def test_fix2_generate_preflights_credentials_before_any_attempt(ledger, monkeypatch, capsys, unavailable):
    from bench import gen
    folder, _ = ledger
    paths = fixture_paths(folder.parent / "live-source")
    events = []
    def token():
        events.append("claude")
        if unavailable == "claude":
            raise RuntimeError("withheld fixture payload")
        return "offline-fixture"
    monkeypatch.setattr(gen, "keychain_token", token)
    def key(*args):
        events.append("eleven")
        if unavailable == "eleven":
            raise SystemExit("withheld fixture payload")
        return "offline-fixture"
    monkeypatch.setattr(run, "key_value", key)
    def prepare(session, *a, **k):
        (session / "src/emotion-chip").mkdir(parents=True)
        return {}
    monkeypatch.setattr(run, "prepare", prepare)
    class Host:
        def __init__(self, *a):
            pass
        def call(self, item, args, **kwargs):
            assert args["mode"] == "e7-credentials"
            return e7.child(dict(args, inputs=str(folder), item_dir=str(item), key_file="unused"), SimpleNamespace())
    monkeypatch.setattr(e7, "E7Host", Host)
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter([("b01", "chip", ["one"]), ("b02", "eva", ["two"])]))
    def cell(session, item, *a):
        events.append(item)
        return {"status": "unvoiced", "clips": []}
    monkeypatch.setattr(e7, "real_cell", cell)
    if unavailable:
        with pytest.raises(RuntimeError, match="credential") as exc:
            e7.generate("fixture-tag", folder, paths=paths)
        assert "withheld" not in str(exc.value)
        assert not (folder / "manifest.json").exists() or not e7.read(folder / "manifest.json")["cells"]
        assert not any(event.startswith("b0") for event in events)
    else:
        e7.generate("fixture-tag", folder, paths=paths)
        assert events == ["claude", "eleven", "b01", "b02"]
    assert capsys.readouterr() == ("", "")


def test_fix2_real_cell_keychain_failure_is_credential(real_case, monkeypatch):
    from bench import gen
    def unavailable():
        raise RuntimeError("withheld fixture payload")
    monkeypatch.setattr(gen, "keychain_token", unavailable)
    _, invoke, calls, _ = real_case
    result = invoke({"completed": True})
    assert result["status"] == "credential" and result["clips"] == [] and calls == []
    assert "withheld" not in json.dumps(result)


def test_fix2_child_missing_key_returns_credential_proof(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "fixture-keys"
    key_file.write_text("UNRELATED=fixture\n")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    proof = e7.child({"mode": "e7-warm", "inputs": str(tmp_path), "item_dir": str(tmp_path),
                      "key_file": str(key_file)}, SimpleNamespace())
    assert proof["credential_failure"] is True and proof["completed"] is False
    assert "ELEVENLABS_API_KEY" not in __import__("os").environ
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error", ["missing API key", "sag: 401", "Unauthorized", "unauthorised", "quota_exceeded"])
@pytest.mark.parametrize("arm", ["chip", "eva"])
@pytest.mark.parametrize("where", ["warm", "turn", "tool"])
def test_fix2_credential_errors_on_both_arms(real_case, arm, where, error):
    _, invoke, _, _ = real_case
    warm = {"error": error} if where == "warm" else None
    proof = {"error": error} if where == "turn" else {
        "completed": True, "turns": [{"tts_results": [{"error": error}]}]}
    assert invoke(proof, arm=arm, warm=warm)["status"] == "credential"


@pytest.mark.parametrize("arm", ["chip", "eva"])
def test_fix2_child_failed_sag_tool_result_is_credential(tmp_path, monkeypatch, arm):
    monkeypatch.setattr(run, "key_value", lambda *a: "offline-fixture")
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", SimpleNamespace(_close_agent=lambda *a: None))
    monkeypatch.setattr(e7, "install_capture", lambda *a: ([], [], []))
    result = {"completed": True, "messages": [{"role": "tool", "name": "text_to_speech",
              "content": '{"error":"sag: 401 Unauthorized"}'}]}
    calls = []
    def conversation(*a, **k):
        calls.append(a)
        return result
    driver = SimpleNamespace(build_agent=lambda cwd: (SimpleNamespace(run_conversation=conversation), None, {}),
        wrap_upstream=lambda item: [], tts_calls=turn.tts_calls, stop_natives=lambda: None)
    proof = e7.child({"mode": "e7-turn", "inputs": str(tmp_path), "key_file": "unused",
                      "item_dir": str(tmp_path), "cwd": str(tmp_path), "arm": arm, "prompts": ["one", "two", "three"]}, driver)
    assert proof["credential_failure"] is True
    assert len(calls) == len(proof["turns"]) == 1


@pytest.mark.parametrize("stream", ["probe.jsonl", "upstream.jsonl"])
@pytest.mark.parametrize("defect", ["no-map", "not-dict", "empty", "missing-fixed", "missing-configured", "hit"])
def test_fix2_evidence_canaries_fail_closed(real_case, monkeypatch, stream, defect):
    _, invoke, _, _ = real_case
    original = run.read_jsonl
    def rows(path):
        result = original(path)
        if path.name == stream:
            row = result[0]
            if defect == "no-map":
                row.pop("canaries")
            elif defect == "not-dict":
                row["canaries"] = []
            elif defect == "empty":
                row["canaries"] = {}
            elif defect.startswith("missing"):
                row["canaries"].pop("fixture" if defect == "missing-configured" else run.FIXED_CANARIES[0])
            else:
                row["canaries"]["fixture"] = True
        return result
    monkeypatch.setattr(run, "read_jsonl", rows)
    assert invoke({"completed": True, "sent_to_voice": [{"clip": "good.mp3"}]})["status"] == "integrity"


@pytest.mark.parametrize("defect", ["upstream_error", "wrong-model", "missing-model"])
def test_fix2_upstream_errors_and_model_fail_closed(real_case, monkeypatch, defect):
    _, invoke, _, _ = real_case
    original = run.read_jsonl
    def rows(path):
        result = original(path)
        if path.name == "upstream.jsonl":
            if defect == "upstream_error":
                result.append(dict(result[0], event="upstream_error"))
            elif defect == "missing-model":
                result[0].pop("model")
            else:
                result[0]["model"] = "wrong-model"
        return result
    monkeypatch.setattr(run, "read_jsonl", rows)
    assert invoke({"completed": True, "sent_to_voice": [{"clip": "good.mp3"}]})["status"] == "integrity"


@pytest.mark.parametrize("exclusive", [False, True])
def test_fix2_write_serialization_failure_never_publishes_partial_json(tmp_path, monkeypatch, exclusive):
    path = tmp_path / "manifest.json"
    if not exclusive:
        e7.write(path, {"original": True})
    def interrupted(value, handle, **kwargs):
        handle.write('{"partial":')
        raise OSError("interrupted serialization")
    monkeypatch.setattr(e7.json, "dump", interrupted)
    with pytest.raises(OSError):
        e7.write(path, {"replacement": True}, exclusive=exclusive)
    assert not path.exists() if exclusive else e7.read(path) == {"original": True}


@pytest.mark.parametrize("exclusive", [False, True])
def test_fix2_write_publishes_via_same_directory_atomic_operation(tmp_path, monkeypatch, exclusive):
    import os
    path = tmp_path / "manifest.json"
    calls = []
    operation = "link" if exclusive else "replace"
    original = getattr(os, operation)
    def publish(source, target):
        source, target = Path(source), Path(target)
        assert source.parent == target.parent and source != target
        assert e7.read(source) == {"complete": True}
        calls.append(operation)
        return original(source, target)
    monkeypatch.setattr(os, operation, publish)
    e7.write(path, {"complete": True}, exclusive=exclusive)
    assert calls == [operation] and e7.read(path) == {"complete": True}
    if exclusive:
        monkeypatch.setattr(os, operation, original)
        with pytest.raises(FileExistsError):
            e7.write(path, {"replacement": True}, exclusive=True)
        assert e7.read(path) == {"complete": True}


def test_fix2_retry_interrupted_before_attempt_does_not_spend_budget(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    generate()
    def interrupted_jobs(reserve):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(e7, "jobs", interrupted_jobs)
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                generate(retry="b01", reason="interrupted before attempt")
    assert len(e7.read(folder / "manifest.json")["cells"]["b01:chip"]) == 1
    for _ in range(2):
        generate(retry="b01", reason="real retry")
    with pytest.raises(ValueError, match="at most 2"):
        generate(retry="b01", reason="third real retry")


@pytest.mark.parametrize("defect", [None, "manifest-status", "missing-manifest-status", "ratings", "missing-ratings", "key"])
def test_fix2_rerun_requires_void_receipt_matching_locked_ratings(tmp_path, defect):
    parent = tmp_path / "parent"
    parent.mkdir()
    key = parent / "key.json"
    e7.write(key, {})
    manifest = {"key_path": str(key), "key_sha256": e7.sha(key), "ratings_sha256": "locked-ratings",
                "locked_at": "2026-10-22T12:00:00Z", "score": {"status": "void"}}
    receipt = {"status": "void", "key_sha256": e7.sha(key), "ratings_sha256": "locked-ratings"}
    if defect == "manifest-status":
        manifest["score"]["status"] = "pass"
    elif defect == "missing-manifest-status":
        manifest.pop("score")
    elif defect == "ratings":
        receipt["ratings_sha256"] = "different-ratings"
    elif defect == "missing-ratings":
        receipt.pop("ratings_sha256")
    elif defect == "key":
        receipt["key_sha256"] = "different-key"
    e7.write(parent / "manifest.json", manifest)
    e7.write(e7.score_receipt_path(key), receipt)
    now = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
    if defect:
        with pytest.raises(ValueError):
            e7.rerun_admission(parent, tmp_path / "new", now)
        assert not (parent / "rerun.json").exists()
    else:
        assert e7.rerun_admission(parent, tmp_path / "new", now) == manifest


def test_fix2_score_receipt_includes_locked_ratings_digest(tmp_path, monkeypatch):
    manifest, key, ratings = [tmp_path / name for name in ("manifest.json", "key.json", "ratings.json")]
    e7.write(key, {})
    e7.write(manifest, {"key_sha256": e7.sha(key)})
    e7.write(ratings, {"docs": {"lock": {"lockedAt": "2026-10-22T12:00:00Z", "ratings": {}}}})
    monkeypatch.setattr(scoring, "score", lambda *a: {"status": "void"})
    scoring.score_files(ratings, key, manifest)
    assert e7.read(e7.score_receipt_path(key))["ratings_sha256"] == e7.read(manifest)["ratings_sha256"] == e7.sha(ratings)


def test_fix5_unlocked_score_does_not_read_key_or_write_anything(tmp_path, monkeypatch):
    ratings, key, manifest = [tmp_path / name for name in ("ratings.json", "missing-key.json", "manifest.json")]
    e7.write(ratings, {"docs": {}})
    e7.write(manifest, {"key_sha256": "unread commitment"})
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    reads = []
    original_read = scoring.read
    def read(path):
        reads.append(path)
        assert path == ratings
        return original_read(path)
    monkeypatch.setattr(scoring, "read", read)
    monkeypatch.setattr(scoring, "sha", lambda *a: pytest.fail("unlocked score hashed a file"))
    monkeypatch.setattr(scoring, "write", lambda *a: pytest.fail("unlocked score wrote a file"))
    assert scoring.score_files(ratings, key, manifest) == {"status": "incomplete"}
    assert reads == [ratings]
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert not e7.score_receipt_path(key).exists()


@pytest.mark.parametrize("defect", ["missing_pair", "missing_mini", "note", "choice", "rating", "acting_a", "acting_b"])
def test_fix5_page_rejects_stale_lock_copy_and_can_lock_after_saving(tmp_path, defect):
    page_check(tmp_path, r'''
build(); await load();
await save('t01', {choice:'a', acting:{a:true, b:true}, note:'latest pair note'});
await save('m01', {rating:4, note:'latest mini note'});
const before = writes.length;
const originalCollection = store.collection;
store.collection = name => ({...originalCollection(name), get: async () => {
  const rows = new Map([...stored].map(([id, doc]) => [id, structuredClone(doc)]));
  if (DEFECT === 'missing_pair') rows.delete('t01');
  if (DEFECT === 'missing_mini') rows.delete('m01');
  if (DEFECT === 'note') rows.get('t01').note = 'older note';
  if (DEFECT === 'choice') rows.get('t01').choice = 'b';
  if (DEFECT === 'rating') rows.get('m01').rating = 2;
  if (DEFECT === 'acting_a') rows.get('t01').acting.a = false;
  if (DEFECT === 'acting_b') rows.get('t01').acting.b = false;
  return {docs: [...rows].map(([id, data]) => snapshot(id, data))};
}});
await lockRatings();
assert.equal(writes.length, before); assert.equal(stored.has('lock'), false);
assert.equal(locked, false); assert.equal(locking, false);
assert.equal(document.querySelector('#status').textContent,
  'Could not lock: your latest ratings are still saving. Try again in a moment.');
assert.ok(document.querySelectorAll('button,input,textarea,select,audio').every(el => !el.disabled));
await save('t01', {note:'edited after failed lock'});
assert.equal(stored.get('t01').note, 'edited after failed lock');
store.collection = originalCollection;
await lockRatings();
assert.equal(locked, true);
assert.equal(writes.filter(w => w.path.endsWith('/lock')).length, 1);
assert.deepEqual(stored.get('lock').ratings, Object.fromEntries([...stored].filter(([id]) => id !== 'lock')));
'''.replace("DEFECT", json.dumps(defect)))


def test_fix4_page_save_after_other_tab_lock_does_not_change_score(tmp_path, built, monkeypatch):
    saved = tmp_path / "late-rating.json"
    page_check(tmp_path, r'''
build(); await load();
let now = '2026-10-22T11:59:00Z';
const NativeDate = Date;
globalThis.Date = class extends NativeDate { constructor(...args) { super(...(args.length ? args : [now])); } };
const originalDoc = store.doc;
let finishRead;
store.doc = path => {
  const ref = originalDoc(path);
  return path.endsWith('/lock') ? {...ref, get: () => new Promise(resolve => {
    finishRead = () => resolve(snapshot('lock', undefined));
  })} : ref;
};
const saving = save('t01', {choice: 'a'});
await drain();
stored.set('lock', {lockedAt: '2026-10-22T12:00:00Z', ratings: {}});
now = '2026-10-22T12:01:00Z'; finishRead(); await saving;
require('node:fs').writeFileSync(SAVED, JSON.stringify(stored.get('t01')));
'''.replace("SAVED", json.dumps(str(saved))))
    _, _, source, _, _, key = built
    ratings = ratings_for(key)
    monkeypatch.setattr(scoring.report, "prereg_check", lambda: {"matches": True})
    baseline = scoring.score(ratings, key, e7.read(source / "manifest.json"))
    doc = e7.read(saved)
    trial = next(t["id"] for t in key["trials"] if t["kind"] == "real")
    ratings["docs"][trial] = dict(doc, trial=trial)
    result = scoring.score(ratings, key, e7.read(source / "manifest.json"))
    assert result == dict(baseline, changed_after_lock=1)


@pytest.mark.parametrize("defect", ["missing_file", "empty_file"])
@pytest.mark.parametrize("also_good", [False, True])
def test_fix2_real_cell_filters_missing_and_empty_clip_files(real_case, defect, also_good):
    folder, invoke, _, joined = real_case
    rows = [{"clip": "bad.mp3", defect: True}] + ([{"clip": "good.mp3"}] if also_good else [])
    result = invoke({"completed": True, "sent_to_voice": rows}, arm="eva")
    assert result["status"] == ("completed" if also_good else "synthesis_failed")
    assert joined == []
    if also_good:
        assert (folder / result["clips"][0]).read_bytes() == b"good.mp3"
    else:
        assert result["clips"] == []


@pytest.mark.parametrize("reply", ["401", "unauthorized", "unauthorised", "missing API key", "quota_exceeded"])
@pytest.mark.parametrize("nested", [False, True])
def test_fix3_completed_reply_uses_only_runner_credential_pattern(real_case, reply, nested):
    _, invoke, _, _ = real_case
    turn_proof = {"completed": True, "final_response": f"The story mentions {reply}.",
                  "sent_to_voice": [{"clip": "good.mp3"}]}
    proof = {"completed": True, "turns": [turn_proof]} if nested else turn_proof
    assert invoke(proof)["status"] == "completed"


def test_fix3_plain_generate_resumes_only_credential_cells_after_preflight(ledger, monkeypatch):
    folder, _ = ledger
    paths = fixture_paths(folder.parent / "recovery-source")
    monkeypatch.setattr(run, "prepare", lambda session, *a, **k: (
        (session / "src/emotion-chip").mkdir(parents=True) or {}))
    cells = [("b01", "chip"), ("b01", "eva"), ("b02", "chip"), ("b03", "eva")]
    monkeypatch.setattr(e7, "jobs", lambda reserve: iter((item, arm, ["fixture"]) for item, arm in cells))
    events, seen = [], []
    available = [True]
    def preflight(*a):
        events.append("preflight")
        if not available[0]:
            raise RuntimeError("credential preflight failed")
    monkeypatch.setattr(e7, "check_credentials", preflight)
    def cell(session, item, arm, *a):
        key = item + ":" + arm
        events.append(key)
        first = key not in seen
        seen.append(key)
        status = "credential" if arm == "eva" and first else "unvoiced" if item == "b02" else "completed"
        clips = []
        if status == "completed":
            clip = session / f"{item}-{arm}.mp3"
            e7.tone(clip)
            clips.append(clip.name)
        return {"status": status, "clips": clips}
    monkeypatch.setattr(e7, "real_cell", cell)
    def generate(**kwargs):
        return e7.generate("fixture-tag", folder, paths=paths, **kwargs)
    with pytest.raises(RuntimeError, match="credential"):
        generate()
    assert events == ["preflight", "b01:chip", "b01:eva"]
    original = e7.read(folder / "manifest.json")
    available[0] = False
    events.clear()
    with pytest.raises(RuntimeError, match="preflight"):
        generate()
    assert events == ["preflight"]
    assert e7.read(folder / "manifest.json") == original
    available[0] = True
    events.clear()
    with pytest.raises(ValueError, match="credential"):
        generate(retry="b01", reason="credentials restored")
    assert events == ["preflight"]
    events.clear()
    with pytest.raises(RuntimeError, match="credential"):
        generate()
    assert events == ["preflight", "b01:eva", "b02:chip", "b03:eva"]
    events.clear()
    manifest = generate()
    assert events == ["preflight", "b03:eva"]
    assert manifest["cells"]["b01:chip"] == original["cells"]["b01:chip"]
    assert manifest["cells"]["b01:eva"][0] == original["cells"]["b01:eva"][0]
    for key in ("b01:eva", "b03:eva"):
        attempts = manifest["cells"][key]
        assert [a["status"] for a in attempts] == ["credential", "completed"]
        assert attempts[-1]["after_credential"] is True
        assert all("retry_reason" not in a for a in attempts)
        assert e7.completed(manifest, *key.split(":"), folder)[0] == attempts[-1]
    assert len(manifest["cells"]["b02:chip"]) == 1
    assert manifest["attempt_counts"]["eva"]["credential"] == 2
    assert manifest["attempt_counts"]["eva"]["completed"] == 2


def test_fix3_credential_resumptions_do_not_consume_retry_budget(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "credential", "clips": []})
    for _ in range(3):
        with pytest.raises(RuntimeError, match="credential"):
            generate()
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "unvoiced", "clips": []})
    generate()
    for reason in ("first retry", "second retry"):
        generate(retry="b01", reason=reason)
    with pytest.raises(ValueError, match="at most 2 retries"):
        generate(retry="b01", reason="third retry")
    manifest = e7.read(folder / "manifest.json")
    attempts = manifest["cells"]["b01:chip"]
    assert [a["status"] for a in attempts] == ["credential"] * 3 + ["unvoiced"] * 3
    assert [a.get("after_credential", False) for a in attempts] == [False, True, True, True, False, False]
    assert [a["retry_reason"] for a in attempts if "retry_reason" in a] == ["first retry", "second retry"]
    assert manifest["attempt_counts"]["chip"]["credential"] == 3
    assert manifest["attempt_counts"]["chip"]["unvoiced"] == 3


def test_fix5_retry_that_hits_credentials_does_not_consume_retry_budget(ledger, monkeypatch):
    folder, generate = ledger
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "turn_error", "clips": []})
    generate()
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "credential", "clips": []})
    with pytest.raises(RuntimeError, match="credential"):
        generate(retry="b01", reason="credential retry")
    with pytest.raises(ValueError, match="credential failures cannot be retried"):
        generate(retry="b01", reason="must use plain replay")
    before_replay = e7.read(folder / "manifest.json")["cells"]["b01:chip"]
    monkeypatch.setattr(e7, "dry_cell", lambda *a: {"status": "turn_error", "clips": []})
    generate()
    generate(retry="b01", reason="first retry")
    generate(retry="b01", reason="second retry")
    with pytest.raises(ValueError, match="at most 2 retries"):
        generate(retry="b01", reason="third retry")
    attempts = e7.read(folder / "manifest.json")["cells"]["b01:chip"]
    assert attempts[:2] == before_replay
    assert [a["status"] for a in attempts] == ["turn_error", "credential", "turn_error", "turn_error", "turn_error"]
    assert attempts[2]["after_credential"] is True
    assert "retry_reason" not in attempts[2]
    assert [a["retry_reason"] for a in attempts if "retry_reason" in a] == [
        "credential retry", "first retry", "second retry"]


def test_fix3_load_paths_expands_canaries_for_real_path_scan(tmp_path, monkeypatch):
    raw = {key: "~/fixture/" + key for key in (*run.PATH_KEYS, *e7.INPUTS, "sag_binary", "eleven_key_file")}
    raw.update(host_label="fixture host", ava_voice_id="fixture-ava", canaries=["~/src", "literal-marker"])
    source = tmp_path / "paths.json"
    e7.write(source, raw)
    monkeypatch.setenv(run.PATHS_ENV, str(source))
    paths = e7.load_paths()
    assert paths["canaries"] == [str(Path("~/src").expanduser()), "literal-marker"]
    assert paths["canaries"] == run.load_paths()["canaries"]
    assert paths["chip_repo"] == Path(raw["chip_repo"]).expanduser()
    assert paths["host_label"] == raw["host_label"] and paths["ava_voice_id"] == raw["ava_voice_id"]
    monkeypatch.setattr(probe, "NEGATIVE_CANARIES", tuple(paths["canaries"]))
    hits = probe.canaries("Source checkout: " + str(Path("~/src").expanduser()))
    assert hits[str(Path("~/src").expanduser())] is True
    assert hits["literal-marker"] is False


def test_build_refuses_a_second_bundle_for_one_session(generated, tmp_path, monkeypatch):
    # A second bundle would let an operator run two listening sessions and score the preferred one.
    _, paths, original = generated
    monkeypatch.setattr(e7, "load_paths", lambda: paths)
    folder = tmp_path / "session"
    shutil.copytree(original, folder)
    e7.build(folder, tmp_path / "bundle-1", tmp_path / "key-1.json")
    with pytest.raises(ValueError, match="already has a bundle"):
        e7.build(folder, tmp_path / "bundle-2", tmp_path / "key-2.json")


def test_generate_refuses_before_any_attempt_without_ffmpeg(generated, tmp_path, monkeypatch):
    _, paths, original = generated
    folder = tmp_path / "session"
    shutil.copytree(original, folder)
    before = (folder / "manifest.json").read_bytes()
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    monkeypatch.setattr(e7.shutil, "which", lambda name: None if name == "ffmpeg" else "/bin/" + name)
    with pytest.raises(RuntimeError, match="ffmpeg is required"):
        e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    assert (folder / "manifest.json").read_bytes() == before
