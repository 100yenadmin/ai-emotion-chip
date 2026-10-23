"""Offline E7 integrity rules 1–11: fakes for Hermes; real ffmpeg on synthetic tones only."""
from datetime import datetime, timezone
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
    root, paths, session = generated
    key_path, bundle = root / "key.json", root / "bundle"
    key = e7.build(session, bundle, key_path)
    return root, paths, session, bundle, key_path, key


def ratings_for(key, wins=15, catches=0):
    counts = {"comparison_a": 0, "comparison_b": 0}
    docs = {"lock": {"lockedAt": "2026-10-22T12:00:00Z"}}
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
    assert e7.sha(e7.HERE / "prompts.json") == e7.PROMPTS_SHA256
    expected = {item: __import__("random").Random(f"{e7.SEED_ROOT}:{item}").randrange(2**32)
                for group in e7.prompts().values() for item in group}
    assert e7.read(folder / "manifest.json")["seeds"] == expected
    assert e7.seed_for("a01") == expected["a01"]


def test_comparison_a_uses_provider_score_and_checks_render():
    assert e7.render_pair("[soft: Hello.]", "[softly] Hello.", render) == ("[softly] Hello.", "Hello.")
    with pytest.raises(RuntimeError, match="recorded provider input"):
        e7.render_pair("[soft: Hello.]", "Hello.", render)
    assert e7.render_pair("Plain.", "Plain.", render) == ("Plain.", "Plain.")


def test_rule5_no_overwrite_and_infra_retry_preserves_attempts(generated, tmp_path, monkeypatch):
    _, paths, source = generated
    folder = tmp_path / "session"
    shutil.copytree(source, folder)
    before = e7.read(folder / "manifest.json")
    monkeypatch.setattr(e7, "frozen_tag", lambda *a: "fixture-commit")
    monkeypatch.setattr(e7, "dry_cell", lambda *a: pytest.fail("recorded cell was regenerated"))
    e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    assert e7.read(folder / "manifest.json") == before
    with pytest.raises(ValueError, match="infrastructure"):
        e7.generate("fixture-tag", folder, dry_run=True, paths=paths, retry_infra="a01", reason="outage")
    before["cells"]["a01:chip"] = [{"status": "infra_failure", "clips": [], "reason": "first outage"}]
    e7.write(folder / "manifest.json", before)
    good = e7.read(source / "manifest.json")["cells"]["a01:chip"][0]
    monkeypatch.setattr(e7, "dry_cell", lambda *a: good)
    e7.generate("fixture-tag", folder, dry_run=True, paths=paths, retry_infra="a01", reason="network recovered")
    attempts = e7.read(folder / "manifest.json")["cells"]["a01:chip"]
    assert [a["status"] for a in attempts] == ["infra_failure", "completed"]
    assert attempts[1]["reason"] == "network recovered"


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


def test_rule9_lock_required_and_late_update_rejected(built):
    _, _, folder, _, _, key = built
    manifest = e7.read(folder / "manifest.json")
    ratings = ratings_for(key)
    del ratings["docs"]["lock"]
    assert scoring.score(ratings, key, manifest) == {"status": "incomplete"}
    ratings = ratings_for(key)
    ratings["docs"][key["trials"][0]["id"]]["updatedAt"] = "2026-10-22T12:01:00Z"
    with pytest.raises(ValueError, match="after lock"):
        scoring.score(ratings, key, manifest)


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
    del ratings["docs"][key["trials"][0]["id"]]
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


def test_mini_session_one_agent_history_and_stitch_capture(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    captured = []
    chip = SimpleNamespace(_prepare=lambda *a, **k: None,
        eleven=SimpleNamespace(synthesize=lambda text, path, **k: Path(path).write_bytes(b"audio")))
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(tts_tool=SimpleNamespace(text_to_speech_tool=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "agent.tts_registry", SimpleNamespace(get_provider=lambda name: chip))
    sent, scores, _ = e7.install_capture({"arm": "chip", "item_dir": str(tmp_path)}, turn)
    history = []
    class Agent:
        def run_conversation(self, message, **kwargs):
            index = len(history)
            assert kwargs == ({} if index == 0 else {"conversation_history": history[-1]})
            chip._prepare("post-cleanup score")
            chip.eleven.synthesize("rendered", tmp_path / f"out/{index}.mp3", seed=42,
                                   previous_request_ids=[f"id-{i}" for i in range(index)])
            history.append([{"role": "user", "content": message}])
            return {"completed": True, "messages": history[-1]}
    e7.conversations(Agent(), ["one", "two", "three"], lambda index, result: captured.append(index))
    assert captured == [0, 1, 2]
    assert [s["previous_request_ids"] for s in sent] == [[], ["id-0"], ["id-0", "id-1"]]
    assert all(s["seed"] == 42 for s in sent)
    assert scores == ["post-cleanup score"] * 3


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


def test_rule9_page_awaits_saves_and_reloads_lock(tmp_path):
    script = (e7.HERE / "template.html").read_text().split("<script>")[1].split("</script>")[0]
    script = script[:script.rindex("\nbuild();")]
    test = r'''
const assert = require('node:assert/strict');
(async () => {
  const events = [];
  let release;
  db = {doc: path => ({set: body => {
    events.push(path);
    if (path.endsWith('/lock')) return Promise.resolve();
    return new Promise(resolve => {release = resolve;});
  }})};
  ready = true;
  const first = save('t01', {choice:'a'});
  const second = save('t02', {choice:'b'});
  const lockingWork = lockRatings();
  await Promise.resolve();
  assert.equal(events.length, 1);
  assert.equal(locked, false);
  release();
  await first; await Promise.resolve(); await Promise.resolve();
  assert.equal(events.length, 2);
  assert.ok(!events.some(p => p.endsWith('/lock')));
  release(); await second; await lockingWork;
  assert.equal(events.length, 3);
  assert.ok(events[2].endsWith('/lock'));
  assert.equal(locked, true);
  await save('t01', {choice:'b'});
  assert.equal(events.length, 3);
  assert.ok(elements.every(el => el.disabled));
  locked = false; ready = false;
  window.claude = {use: async () => ({
    doc: path => ({get: async () => ({data: () => ({lockedAt:'2026-10-22T00:00:00Z'})})}),
    collection: name => ({get: async () => ({docs:[]})})
  })};
  await load();
  assert.equal(locked, true);
  assert.ok(elements.every(el => el.disabled));
  console.log('lock behavior verified');
})().catch(e => {console.error(e); process.exitCode = 1;});
'''
    dom = r'''
const elements = [{disabled:false,tagName:'BUTTON'}, {disabled:false,tagName:'INPUT'}];
const status = {textContent:''};
const document = {querySelector:s=>status, querySelectorAll:s=>elements, getElementById:s=>null};
const window = {};
'''
    path = tmp_path / "page-test.js"
    path.write_text(dom + script + test)
    result = subprocess.run([shutil.which("node"), str(path)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "lock behavior verified"


def test_rule11_rerun_gate_and_byte_identical_real_pairs(built, tmp_path, monkeypatch):
    _, paths, folder, bundle, key_path, first_key = built
    parent = tmp_path / "first"
    shutil.copytree(folder, parent)
    manifest = e7.read(parent / "manifest.json")
    new = tmp_path / "rerun"
    with pytest.raises(ValueError, match="lock"):
        e7.rerun_admission(parent, new)
    manifest["locked_at"] = "2026-10-22T12:00:00Z"
    e7.write(parent / "manifest.json", manifest)
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
    count = [0]
    class Agent:
        session_id = "fixture-session"
        def run_conversation(self, prompt, **kwargs):
            count[0] += 1
            provider.synthesize("[soft: Hello.]", tmp_path / f"out/turn-{count[0]}.mp3")
            return {"completed": True, "messages": [{"role": "assistant", "content": "fixture"}]}
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
    result = e7.child(args, driver)
    assert result["completed"] is False and result["error_type"] == "RuntimeError"


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


def test_generate_unvoiced_and_infra_failure_are_recorded(tmp_path, monkeypatch):
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
    with pytest.raises(RuntimeError, match="infra_failure"):
        e7.generate("fixture-tag", folder, dry_run=True, paths=paths)
    manifest = e7.read(folder / "manifest.json")
    assert manifest["cells"]["a01:chip"][0]["status"] == "infra_failure"
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
        ratings["docs"][trial["id"]]["choice"] = "none"
    result = scoring.score(ratings, key, manifest)
    assert result["status"] == "fail"
    assert result["comparisons"]["comparison_a"]["chip_preferred"] == 20
    assert result["comparisons"]["comparison_b"]["chip_preferred"] == 14
    for kind in ("catch", "mini"):
        ratings = ratings_for(key)
        trial = next(t for t in key["trials"] if t["kind"] == kind)
        del ratings["docs"][trial["id"]]
        assert scoring.score(ratings, key, manifest)["status"] == "incomplete"
    ratings = ratings_for(key)
    trial = next(t for t in key["trials"] if t["comparison"] == "comparison_b")
    ratings["docs"][trial["id"]]["acting"]["a"] = True
    result = scoring.score(ratings, key, manifest)
    assert result["comparisons"]["comparison_b"]["acting"][trial["sides"]["a"]] == 1
    assert result["notes"] == 47
