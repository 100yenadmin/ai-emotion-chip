"""Offline tests for the Hermes-native listening runner: no Hermes host, model, keychain, Fish or sandbox."""
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from bench.hermes import run
from bench.hermes.bench_probe import probe

PROBE_DIR = Path(run.__file__).parent / "bench_probe"


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external processes are forbidden in unit tests")
    monkeypatch.setattr(run.subprocess, "run", forbidden)
    monkeypatch.setattr(run.subprocess, "Popen", forbidden)
    # main() fills the Ava voice from the paths file; tests use a fixture id.
    monkeypatch.setitem(run.AVA, "eleven_voice_id", "ava-voice-id")


def pre(sha="h1", chip=True, skill=True, hits=(), call=1, chars=100):
    return {"event": "pre_api_request", "api_call_count": call, "system_sha256": sha, "system_chars": chars,
            "chip_section": chip, "skill_row": skill,
            "canaries": {c: c in hits for c in probe.NEGATIVE_CANARIES}}


def post(tokens, call=1):
    return {"event": "post_api_request", "api_call_count": call, "usage": {"prompt_tokens": tokens}}


def up(chip=True, hits=(), model=run.MODEL):
    return {"event": "upstream", "chip_section": chip, "model": model,
            "canaries": {c: c in hits for c in probe.NEGATIVE_CANARIES}}


DB_OK = {"usage_rows": [{"task": ""}]}


# --- config and environment -------------------------------------------------------------------------------------

def test_config_isolation_keys_are_identical_in_both_arms(tmp_path):
    on, off = run.hermes_config("on", tmp_path / "c"), run.hermes_config("off", tmp_path / "c")
    for key in ("auth", "auxiliary", "memory", "curator", "context", "model", "agent", "stt"):
        assert on[key] == off[key]
    assert on["auth"] == {"adopt_external_logins": False}
    assert on["auxiliary"]["title_generation"] == {"enabled": False}
    assert on["auxiliary"]["background_review"] == {"enabled": False}
    assert on["memory"] == {"memory_enabled": False, "user_profile_enabled": False}
    assert on["curator"] == {"enabled": False} and on["context"] == {"engine": "compressor"}
    assert on["model"] == {"provider": run.PROVIDER, "default": "claude-opus-5-5"}
    assert on["agent"] == {"reasoning_effort": "medium"}
    assert on["tts"]["fish-audio"] == off["tts"]["fish-audio"] == {"model": "s2.1-pro"}
    provider = on["plugins"]["entries"][run.PROVIDER]["settings"]
    assert provider == {"claude_config_dir": str(tmp_path / "c"), "session_mode": True}


def test_arm_on_speaks_through_the_chip_and_arm_off_never_loads_it(tmp_path):
    on, off = run.hermes_config("on", Path("/c")), run.hermes_config("off", Path("/c"))
    assert on["tts"]["provider"] == "emotion-chip"
    assert "emotion-chip" in on["plugins"]["enabled"]
    assert on["plugins"]["entries"]["emotion-chip"]["settings"] == {"guide_mode": "generate"}
    assert off["tts"]["provider"] == "fish-audio"
    assert "emotion-chip" not in json.dumps(off)
    with pytest.raises(ValueError):
        run.hermes_config("both", Path("/c"))


def test_child_env_is_built_from_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    env = run.child_env(tmp_path, "fishkey", "tok")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and env["FISH_API_KEY"] == "fishkey"
    assert not [k for k in env if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
                and k not in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}]
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert "CLAUDECODE" not in env
    for key in ("HOME", "TMPDIR", "HERMES_HOME", "XDG_CACHE_HOME", "BENCH_PROBE_OUT"):
        assert env[key].startswith(str(tmp_path))
    assert env["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert env["PATH"].split(":")[0] == f"{Path.home()}/.local/bin"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in run.child_env(tmp_path, "k")


def test_scrub_and_prompts():
    assert run.scrub("a SECRET b Bearer xyz", ["SECRET", None]) == "a [redacted] b [redacted]"
    assert run.prompt_for("p14") == "Whisper me a secret about the stars. Reply as a voice message."
    assert run.select_items("smoke") == ["p01", "p05", "p11"]
    assert len(run.select_items("full")) == 20
    assert run.select_items("p03,p04") == ["p03", "p04"]
    with pytest.raises(SystemExit):
        run.select_items("p99")


def test_key_value_and_tree_sha(tmp_path):
    (tmp_path / "k.env").write_text("OTHER=1\nFISH_API_KEY='abc'\n")
    assert run.key_value(tmp_path / "k.env", "FISH_API_KEY") == "abc"
    with pytest.raises(SystemExit):
        run.key_value(tmp_path / "k.env", "MISSING")
    tree = tmp_path / "t"
    (tree / "__pycache__").mkdir(parents=True)
    (tree / "a.py").write_text("x")
    first = run.tree_sha256(tree)
    (tree / "__pycache__" / "a.pyc").write_bytes(b"junk")
    assert run.tree_sha256(tree) == first
    (tree / "a.py").write_text("y")
    assert run.tree_sha256(tree) != first
    second = run.tree_sha256(tree, skip=run.HOST_SKIP)
    (tree / ".venv").mkdir()
    (tree / ".venv" / "lib.py").write_text("installed")
    assert run.tree_sha256(tree, skip=run.HOST_SKIP) == second and run.tree_sha256(tree) != second


# --- probe ------------------------------------------------------------------------------------------------------

def test_normalise_drops_timestamp_lines_and_item_dir():
    a = "You are Hermes.\nConversation started: Friday, October 23, 2026 (KST)\nModel: m\nSee /x/items/on/p01/home"
    b = "You are Hermes.\nConversation started: Saturday, October 24, 2026 (KST)\nModel: m\nSee /x/items/on/p05/home"
    assert probe.normalise(a, "/x/items/on/p01") == probe.normalise(b, "/x/items/on/p05")
    assert "Conversation started" not in probe.normalise(a)
    assert probe.normalise([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]) == "one\ntwo"


def test_request_row_flags_canaries_and_sections(tmp_path):
    kwargs = {"system_prompt": "x\n## Voice delivery (Hermes Emotion Chip)\nemotion-chip-voice: d",
              "request_messages": [{"role": "user", "content": "read AGENTS.md"}], "api_call_count": 1,
              "request": {"body": {"tools": [{"function": {"name": "text_to_speech"}},
                                             {"function": {"name": "skills_list"}}]}},
              "approx_input_tokens": 1234}
    row = probe.request_row(kwargs)
    assert row["chip_section"] and row["skill_row"] and row["tool_count"] == 2
    assert row["canaries"]["AGENTS.md"] and not row["canaries"]["Lane Contract"]
    assert row["approx_input_tokens"] == 1234
    assert "read AGENTS.md" not in json.dumps(row)  # hashes and flags only, never text
    clean = probe.request_row({"system_prompt": "plain", "request_messages": []})
    assert not any(clean["canaries"].values()) and not clean["chip_section"]


def test_response_and_upstream_rows():
    row = probe.response_row({"api_call_count": 1, "usage": {"prompt_tokens": 900, "model": "x", "flag": True},
                              "finish_reason": "stop"})
    assert row["usage"] == {"prompt_tokens": 900}
    body = json.dumps({"model": "claude-opus-5-5", "system": [{"type": "text", "text": "Lane Contract here"}],
                       "messages": [], "tools": [{"name": "mcp__hermes__text_to_speech"}]}).encode()
    up_row = probe.upstream_row(body)
    assert up_row["canaries"]["Lane Contract"] and up_row["tool_count"] == 1 and up_row["model"] == "claude-opus-5-5"
    bad = probe.upstream_row(b"not json AGENTS.md")
    assert bad["event"] == "upstream_error" and bad["error"] == "undecodable body" and bad["canaries"]["AGENTS.md"]
    no_system = probe.upstream_row(json.dumps({"model": "m", "messages": []}).encode())
    assert no_system["event"] == "upstream_error" and "system_sha256" not in no_system


def test_probe_plugin_registers_both_hooks_and_appends_jsonl(tmp_path, monkeypatch):
    from bench.hermes import bench_probe as module
    hooks = {}
    module.register(type("Ctx", (), {"register_hook": lambda self, n, f: hooks.__setitem__(n, f)})())
    assert set(hooks) == {"pre_api_request", "post_api_request"}
    out = tmp_path / "probe.jsonl"
    monkeypatch.setenv("BENCH_PROBE_OUT", str(out))
    hooks["pre_api_request"](system_prompt="s", request_messages=[], api_call_count=1)
    hooks["post_api_request"](usage={"prompt_tokens": 5}, api_call_count=1)
    rows = run.read_jsonl(out)
    assert [r["event"] for r in rows] == ["pre_api_request", "post_api_request"]
    manifest = (PROBE_DIR / "plugin.yaml").read_text()
    assert "name: bench-probe" in manifest and "pre_api_request" in manifest


# --- asserts ----------------------------------------------------------------------------------------------------

def test_cell_asserts_pass_for_clean_cells():
    on = run.cell_asserts("on", [pre(), post(100)], [up()], DB_OK, True)
    off = run.cell_asserts("off", [pre(chip=False, skill=False)], [up(chip=False)], DB_OK, True)
    assert all(a["pass"] for a in on.values()) and all(a["pass"] for a in off.values())


@pytest.mark.parametrize("arm,probe_rows,upstream,db,memories,failing", [
    ("on", [pre(hits=("round-trip rule",))], [up()], DB_OK, True, "1_no_negative_canary"),
    ("on", [pre()], [up(hits=("AGENTS.md",))], DB_OK, True, "1_no_negative_canary"),
    ("on", [pre()], [], DB_OK, True, "1_no_negative_canary"),
    ("on", [pre()], [up(), {"event": "upstream_error"}], DB_OK, True, "1_no_negative_canary"),
    ("on", [pre()], [up(), up(model="claude-fable-5-1")], DB_OK, True, "1_no_negative_canary"),
    ("on", [], [up()], DB_OK, True, "1_no_negative_canary"),
    ("on", [pre(skill=False)], [up()], DB_OK, True, "2_positive_canary"),
    ("on", [pre()], [up(chip=False)], DB_OK, True, "2_positive_canary"),
    ("off", [pre()], [up()], DB_OK, True, "2_positive_canary"),
    ("off", [pre(chip=False, skill=False)], [up(chip=False)], {"usage_rows": []}, True, "5_db_and_memory"),
    ("off", [pre(chip=False, skill=False)], [up(chip=False)], {"usage_rows": [{"task": ""}, {"task": ""}]}, True,
     "5_db_and_memory"),
    ("off", [pre(chip=False, skill=False)], [up(chip=False)], DB_OK, False, "5_db_and_memory"),
])
def test_cell_asserts_fail_closed(arm, probe_rows, upstream, db, memories, failing):
    asserts = run.cell_asserts(arm, probe_rows, upstream, db, memories)
    assert not asserts[failing]["pass"]


def test_arm_asserts_hash_and_token_band():
    def cell(rows):
        return {"probe": rows, "asserts": {}}
    cells = {"p01": cell([pre("A"), post(1000)]), "p05": cell([pre("A"), pre("A", call=2), post(1030)]),
             "p11": cell([pre("B"), post(1100)])}
    refs = run.arm_asserts(cells)
    assert refs == {"reference_hash": "A", "call1_median": 1030}
    assert cells["p01"]["asserts"]["3_constant_system_hash"]["pass"]
    assert cells["p05"]["asserts"]["3_constant_system_hash"]["pass"]
    assert not cells["p11"]["asserts"]["3_constant_system_hash"]["pass"]
    assert cells["p01"]["asserts"]["4_call1_tokens_within_band"]["pass"]   # 1000 is 2.9% off 1030
    assert not cells["p11"]["asserts"]["4_call1_tokens_within_band"]["pass"]  # 1100 is 6.8% off
    missing = {"p01": cell([pre("A")])}
    run.arm_asserts(missing)
    assert not missing["p01"]["asserts"]["4_call1_tokens_within_band"]["pass"]


def test_first_call_tokens_sums_buckets_without_prompt_tokens():
    rows = [{"event": "post_api_request", "api_call_count": 1,
             "usage": {"input_tokens": 10, "cache_read_tokens": 5, "cache_write_tokens": 2}}]
    assert run.first_call_tokens(rows) == 17
    assert run.first_call_tokens([post(9, call=2)]) is None


def test_db_summary_reads_usage_and_prompt_by_hash(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, system_prompt_hash TEXT);
CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, api_call_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER);
INSERT INTO system_prompts VALUES ('h', 'x ## Voice delivery (Hermes Emotion Chip) y');
INSERT INTO sessions VALUES ('s1', 'h');
INSERT INTO session_model_usage VALUES ('s1', 'claude-opus-5-5', '', 2, 10, 20, 30, 40);
""")
    out = run.db_summary(db_path)
    assert out["sessions"] == 1 and out["system_prompt_has_chip_section"]
    assert out["usage_rows"] == [{"model": "claude-opus-5-5", "task": "", "api_call_count": 2, "input_tokens": 10,
                                  "output_tokens": 20, "cache_read_tokens": 30, "cache_write_tokens": 40}]
    assert run.db_summary(tmp_path / "missing.db")["exists"] is False


def test_memories_empty(tmp_path):
    assert run.memories_empty(tmp_path)
    (tmp_path / "memories").mkdir()
    assert run.memories_empty(tmp_path)
    (tmp_path / "memories" / "MEMORY.md").write_text("x")
    assert not run.memories_empty(tmp_path)


# --- leak check, pairs, report ----------------------------------------------------------------------------------

def test_leak_words_reports_tag_words_read_aloud():
    sent = "[excited] It works! [laugh] Let's ship it. [soft: Thank you.]"
    assert run.leak_words(sent, "Excited. It works! Laugh. Let's ship it. Thank you.") == ["excited", "laugh"]
    assert run.leak_words(sent, "It works, let's ship it, thank you.") == []
    assert run.leak_words("It was at 3, not 2.", "It was at three, not two.") == []
    assert run.spoken("[[register: steady]] Hi [pause] there") .split() == ["Hi", "there"]


def test_blind_pairs_are_seeded_balanced_and_skip_unvoiced():
    cells = {"on": {"p01": {"clip": "clips/p01-on.mp3"}, "p05": {"clip": None}},
             "off": {"p01": {"clip": "clips/p01-off.mp3"}, "p05": {"clip": "clips/p05-off.mp3"}}}
    pairs, key, clips = run.blind_pairs(["p01", "p05"], cells, seed=7)
    assert [p["item"] for p in pairs] == ["p01"]
    assert sorted(key["p01"].values()) == ["off", "on"]
    labels = json.dumps([{k: v for k, v in p.items() if k != "prompt"} for p in pairs])
    assert "-on" not in labels and "-off" not in labels
    assert clips["p01"][[l for l, a in key["p01"].items() if a == "on"][0]] == "clips/p01-on.mp3"
    assert run.blind_pairs(["p01", "p05"], cells, seed=7)[1] == key


def test_render_report_has_one_row_per_cell():
    cell = {"item": "p01", "arm": "on", "asserts": {"1_no_negative_canary": {"pass": True}},
            "tts_calls": [{"text": "[relieved] It passed."}], "sent_to_voice": [{"text": "(relieved) It passed."}],
            "leak_words": ["relieved"], "clip": "clips/p01-on.mp3", "final_response": "Done | sent"}
    unvoiced = dict(cell, arm="off", tts_calls=[], sent_to_voice=[], leak_words=[], clip=None,
                    asserts={"5_db_and_memory": {"pass": False}})
    manifest = {"run_id": "r1", "claim_class": "local_smoke", "proof_boundary": "pb", "does_not_prove": "dnp",
                "shas": {"hermes": "h", "fish_audio": "f", "emotion_chip": "e", "provider_tree": "p",
                         "bench_probe_tree": "b"}, "model": "m", "effort": "medium", "fish_model": "s2.1-pro",
                "arms": {"on": {"cells": 1, "voiced": 1, "isolation_passes": 1, "call1_median": 9000},
                         "off": {"cells": 1, "voiced": 0, "isolation_passes": 0, "call1_median": 8000}},
                "token_delta": {"tokens": 1000, "system_chars": 3400, "system_chars_as_tokens": 850},
                "cells": [cell, unvoiced]}
    text = run.render_report(manifest)
    assert "| p01 | on | - | pass | [relieved] It passed. | (relieved) It passed. | not checked | clips/p01-on.mp3 |" \
        in text
    assert "| p01 | off | - | FAIL 5_db_and_memory | (no text_to_speech call) | - | - | - |" in text
    checked = run.render_report(dict(manifest, cells=[dict(cell, asr={"success": True}, turn={"pass": True}),
                                                      dict(cell, asr={"success": True}, leak_words=[],
                                                           turn={"pass": False})]))
    assert "| p01 | on | ok | pass | [relieved] It passed. | (relieved) It passed. | relieved |" in checked
    assert "| p01 | on | FAILED | pass | [relieved] It passed. | (relieved) It passed. | none |" in checked
    assert "Done \\| sent" in text and "dnp" in text
    assert "| What the voice received |" in text and "Fish s2.1-pro, default voice" in text
    ava = run.render_report(dict(manifest, voice_label=run.voice_label("ava"), stopped="credential failure at p05 off"))
    assert "ava-voice-id" in ava and "**Stopped early:** credential failure at p05 off" in ava


def test_turn_driver_extracts_text_to_speech_calls():
    spec = importlib.util.spec_from_file_location("hermes_turn_driver", Path(run.__file__).parent / "turn.py")
    turn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(turn)
    messages = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "text_to_speech", "arguments": json.dumps({"text": "[warm] Hi."})}},
        {"function": {"name": "skills_list", "arguments": "{}"}}]},
        {"role": "tool", "name": "text_to_speech", "content": json.dumps({"success": True, "file_path": "/a.mp3"})},
        {"role": "assistant", "content": "Sent."}]
    calls, results = turn.tts_calls(messages)
    assert calls == [{"text": "[warm] Hi.", "arguments": ["text"]}]
    assert results == [{"success": True}]
    assert turn.TOOLSETS == ["skills"]
    assert turn.LISTENING_TOOLSETS == ["tts", "skills"]


# --- machine paths ----------------------------------------------------------------------------------------------

def test_paths_come_from_the_local_file(tmp_path):
    with pytest.raises(SystemExit, match="EMOTION_CHIP_BENCH_PATHS"):
        run.load_paths({})
    example = json.loads((Path(run.HERE) / "paths.example.json").read_text())
    config = tmp_path / "paths.json"
    config.write_text(json.dumps({**example, "runs_root": str(tmp_path / "runs")}))
    paths = run.load_paths({run.PATHS_ENV: str(config)})
    assert paths["runs_root"] == tmp_path / "runs" and paths["host_label"] == example["host_label"]
    assert paths["canaries"] == [str(Path("~/src").expanduser())] + example["canaries"][1:]
    assert set(run.PATH_KEYS) <= set(paths)
    for empty in ([], None, [""]):
        config.write_text(json.dumps({**example, "canaries": empty}))
        with pytest.raises(SystemExit, match="canaries"):
            run.load_paths({run.PATHS_ENV: str(config)})
    config.write_text(json.dumps({k: v for k, v in example.items() if k != "chip_repo"}))
    with pytest.raises(SystemExit, match="chip_repo"):
        run.load_paths({run.PATHS_ENV: str(config)})
    # The Ava voice is local configuration too; it is optional and only --voice ava needs it.
    assert paths["eleven_voice_id"] == example["eleven_voice_id"]
    config.write_text(json.dumps({k: v for k, v in example.items() if k != "eleven_voice_id"}))
    assert run.load_paths({run.PATHS_ENV: str(config)})["eleven_voice_id"] is None


def test_ava_mode_needs_a_configured_voice(tmp_path, monkeypatch):
    monkeypatch.setitem(run.AVA, "eleven_voice_id", "")
    with pytest.raises(ValueError, match="eleven_voice_id"):
        run.hermes_config("on", tmp_path, "ava")
    assert run.hermes_config("on", tmp_path)["tts"]["provider"] == "emotion-chip"


def test_machine_canaries_reach_the_probe(tmp_path, monkeypatch):
    env = run.child_env(tmp_path, "k", canaries=["/home/me/src"])
    assert json.loads(env["BENCH_PROBE_CANARIES"]) == ["/home/me/src"]
    monkeypatch.setenv("BENCH_PROBE_CANARIES", env["BENCH_PROBE_CANARIES"])
    reloaded = importlib.reload(probe)
    try:
        assert reloaded.canaries("cwd is /home/me/src/x")["/home/me/src"] is True
    finally:
        monkeypatch.delenv("BENCH_PROBE_CANARIES")
        importlib.reload(probe)


# --- voice ava (eva's ElevenLabs voice) -------------------------------------------------------------------------

def test_ava_arms_share_the_audio_path_and_differ_only_in_guide_mode(tmp_path):
    on, off = run.hermes_config("on", tmp_path, "ava"), run.hermes_config("off", tmp_path, "ava")
    for config, mode in ((on, "generate"), (off, "defer")):
        assert config["tts"]["provider"] == "emotion-chip"
        assert "emotion-chip" in config["plugins"]["enabled"]
        assert config["plugins"]["entries"]["emotion-chip"]["settings"] == {
            "backend": "eleven", "eleven_voice_id": "ava-voice-id", "eleven_model": "eleven_v4",
            "eleven_style": 0.30, "eleven_speed": 0.90, "guide_mode": mode}
    # The config pins the voice in tts.elevenlabs too, for plugin builds that ranked Hermes' stock voice first.
    assert on["tts"]["elevenlabs"] == {"voice_id": "ava-voice-id", "model_id": "eleven_v4"}
    assert "elevenlabs" not in run.hermes_config("on", tmp_path)["tts"]
    off["plugins"]["entries"]["emotion-chip"]["settings"]["guide_mode"] = "generate"
    assert on == off
    fish = run.hermes_config("on", tmp_path, "fish")
    assert fish == run.hermes_config("on", tmp_path) and fish["plugins"]["entries"]["emotion-chip"]["settings"] == {
        "guide_mode": "generate"}
    assert run.plugin_names("off", "ava") == run.plugin_names("on", "ava") == run.plugin_names("on")
    assert "emotion-chip" not in run.plugin_names("off")
    with pytest.raises(ValueError):
        run.hermes_config("on", tmp_path, "eleven")


def test_eleven_key_reaches_only_the_child_env_and_is_scrubbed(tmp_path, monkeypatch):
    assert "ELEVENLABS_API_KEY" not in run.child_env(tmp_path, "fishkey", "tok")
    env = run.child_env(tmp_path, "fishkey", "tok", eleven_key="sk_eleven_secret")
    assert env["ELEVENLABS_API_KEY"] == "sk_eleven_secret"
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return type("P", (), {"returncode": 1, "stdout": "boom sk_eleven_secret fishkey tok\n", "stderr": ""})()
    monkeypatch.setattr(run.subprocess, "run", fake_run)
    item = tmp_path / "item"
    item.mkdir()
    host = run.Host(tmp_path, "fishkey", {"host": tmp_path, "canaries": []}, eleven_key="sk_eleven_secret")
    out = host.call(item, {"mode": "turn"}, token="tok")
    assert seen["env"]["ELEVENLABS_API_KEY"] == "sk_eleven_secret"
    log = (item / "log-turn.txt").read_text()
    for secret in ("sk_eleven_secret", "fishkey", "tok"):
        assert secret not in log and secret not in out["error"]
    assert run.scrub("x sk_eleven_secret y", ["fishkey", "sk_eleven_secret", None]) == "x [redacted] y"


def test_credential_failures_are_recognised():
    assert run.credential_failure({"final_response": "upstream said: OAuth access token has been revoked."})
    assert run.credential_failure({"tts_results": [{"success": False, "error": "ElevenLabs HTTP 401: invalid"}]})
    assert run.credential_failure({"error": "Claude subscription credential unavailable"})
    assert run.credential_failure({"final_response": "Here is your forecast.", "tts_results": [{"success": True}]}) \
        is None


def test_voice_recorder_keeps_what_the_voice_received(tmp_path):
    spec = importlib.util.spec_from_file_location("hermes_turn_driver_rec", Path(run.__file__).parent / "turn.py")
    turn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(turn)
    (tmp_path / "out").mkdir()

    def eleven(text, output_path, *, voice, model, params, format="mp3"):
        Path(output_path).write_bytes(b"ID3eleven")
        return "req-123"  # the ElevenLabs client returns a request id, not a path

    def fish(text, output_path, **kwargs):
        Path(output_path).write_bytes(b"ID3fish")
        return str(output_path)
    sent = []
    record = turn._recorder(tmp_path, sent, eleven, "eleven", lambda k: {"model": k.get("model"),
                                                                          "params": k.get("params")})
    assert record("[warm] Hi.", tmp_path / "a.mp3", voice="v", model="eleven_v4", params={"style": 0.3}) == "req-123"
    record_fish = turn._recorder(tmp_path, sent, fish, "fish", lambda k: {})
    record_fish("(warm) Hi.", tmp_path / "b.mp3")
    assert [r["text"] for r in sent] == ["[warm] Hi.", "(warm) Hi."]
    assert sent[0]["clip"] == "eleven-1.mp3" and (tmp_path / "out/eleven-1.mp3").read_bytes() == b"ID3eleven"
    assert sent[0]["params"] == {"style": 0.3} and sent[1]["clip"] == "fish-2.mp3"


def test_ava_summary_names_the_voice_and_the_stop():
    cells = {"on": {}, "off": {}}
    refs = {arm: {"call1_median": None, "reference_hash": None} for arm in run.ARMS}
    manifest = run.summarise("r", {}, [], cells, refs, "ava", "credential failure at p01 on: HTTP 401")
    assert manifest["voice"] == "ava" and manifest["stopped"].endswith("HTTP 401")
    assert "ElevenLabs synthesis" in manifest["proof_boundary"] and "Fish," in manifest["does_not_prove"]
    assert "This dir is unblinded" in manifest["proof_boundary"]
    assert "It is not an E7 session: no catch trials" in manifest["does_not_prove"]
    assert manifest["does_not_prove"].endswith("ElevenLabs seeds are not locked.")
    fish = run.summarise("r", {}, [], cells, refs)
    assert fish["voice"] == "fish" and "not an E7 session" in fish["does_not_prove"]
    assert "seeds" not in fish["does_not_prove"]


# --- review fixes: required canaries, fail-closed evidence, turn gate, early stop ---------------------------------

def test_fixed_canaries_and_required_machine_canaries():
    assert probe.FIXED_CANARIES == ("round-trip rule", "Lane Contract", "AGENTS.md")
    assert set(probe.FIXED_CANARIES) <= set(probe.NEGATIVE_CANARIES)
    required = probe.FIXED_CANARIES + ("/home/me/src",)
    # A row computed without the machine canary cannot pass: the canary was never checked.
    asserts = run.cell_asserts("on", [pre()], [up()], DB_OK, True, required)
    assert not asserts["1_no_negative_canary"]["pass"]
    assert asserts["1_no_negative_canary"]["unchecked_canaries"] == ["/home/me/src"]
    full = {c: False for c in required}
    ok = run.cell_asserts("on", [dict(pre(), canaries=full)], [dict(up(), canaries=full)], DB_OK, True, required)
    assert ok["1_no_negative_canary"]["pass"]


def test_warm_mode_gets_the_canaries_too(tmp_path):
    env = run.child_env(tmp_path, "k", probe=False, canaries=["/home/me/src"])
    assert json.loads(env["BENCH_PROBE_CANARIES"]) == ["/home/me/src"] and "BENCH_PROBE_OUT" not in env


def test_missing_system_prompt_is_invalid_evidence():
    row = probe.request_row({"system_prompt": None, "request_messages": []})
    assert row["system_sha256"] is None and row["invalid"]
    assert probe.request_row({"system_prompt": "  \n"})["system_sha256"] is None
    asserts = run.cell_asserts("off", [dict(pre(chip=False, skill=False), system_sha256=None, invalid="x")],
                               [up(chip=False)], DB_OK, True)
    assert not asserts["1_no_negative_canary"]["pass"] and not asserts["2_positive_canary"]["pass"]
    cells = {"p01": {"probe": [dict(pre(), system_sha256=None), post(100)], "asserts": {}}}
    run.arm_asserts(cells)
    assert not cells["p01"]["asserts"]["3_constant_system_hash"]["pass"]


def test_unknown_input_usage_stays_unknown_and_fails_assert_4():
    only_output = [{"event": "post_api_request", "api_call_count": 1, "usage": {"output_tokens": 50}}]
    assert run.first_call_tokens(only_output) is None
    cells = {i: {"probe": [pre("A")] + only_output, "asserts": {}} for i in ("p01", "p05")}
    refs = run.arm_asserts(cells)
    assert refs["call1_median"] is None
    assert not any(c["asserts"]["4_call1_tokens_within_band"]["pass"] for c in cells.values())
    zero = {"p01": {"probe": [pre("A"), post(0)], "asserts": {}}}
    run.arm_asserts(zero)
    assert not zero["p01"]["asserts"]["4_call1_tokens_within_band"]["pass"]


def test_upstream_error_row_fails_assert_1():
    bad = probe.upstream_row(b"\xff\xfe not json")
    asserts = run.cell_asserts("off", [pre(chip=False, skill=False)], [up(chip=False), bad], DB_OK, True)
    assert not asserts["1_no_negative_canary"]["pass"] and asserts["1_no_negative_canary"]["upstream_errors"] == 1


@pytest.mark.parametrize("result,ok", [
    ({"completed": True, "failed": False, "error": None}, True),
    ({"completed": True, "failed": False, "error": None, "tts_calls": []}, True),  # unvoiced is not failed
    ({"completed": False, "failed": True, "error": "HTTP 401"}, False),
    ({"completed": False, "failed": False}, False),
    ({"completed": True, "failed": True}, False),
    ({"error": "Traceback ...", "rc": 1}, False),
    ({"error": "no PROOF", "rc": 0}, False),
])
def test_turn_gate(result, ok):
    assert run.turn_gate(result)["pass"] is ok


def cell(item, arm, turn=True, asserts=True, credential=None):
    return {"item": item, "arm": arm, "voiced": True, "turn": {"pass": turn}, "credential_failure": credential,
            "asserts": {"1_no_negative_canary": {"pass": asserts}}}


@pytest.mark.parametrize("bad,reason", [
    (dict(turn=False), "turn failed at p01 off"),
    (dict(asserts=False), "isolation asserts failed at p01 off: 1_no_negative_canary"),
    (dict(credential="HTTP 401"), "credential failure at p01 off: HTTP 401"),
])
def test_run_cells_stops_paid_work_at_the_first_bad_cell(bad, reason):
    calls = []

    def run_one(arm, item_id):
        calls.append((item_id, arm))
        return cell(item_id, arm, **(bad if (item_id, arm) == ("p01", "off") else {}))
    cells, stopped = run.run_cells(["p01", "p05"], run_one)
    assert stopped == reason and calls == [("p01", "on"), ("p01", "off")]
    assert set(cells["on"]) == set(cells["off"]) == {"p01"}


def test_run_cells_stops_when_the_keychain_fails():
    def run_one(arm, item_id):
        if arm == "off":
            raise RuntimeError("Claude subscription credential unavailable")
        return cell(item_id, arm)
    cells, stopped = run.run_cells(["p01", "p05"], run_one)
    assert stopped.startswith("credential failure before p01 off") and list(cells["on"]) == ["p01"]
    assert run.run_cells(["p01"], lambda arm, i: cell(i, arm)) [1] is None


def test_run_cells_keeps_earlier_cells_when_a_cell_breaks():
    import subprocess
    def run_one(arm, item_id):
        if item_id == "p05":
            raise subprocess.CalledProcessError(1, ["cp", "-c"])
        return cell(item_id, arm)
    cells, stopped = run.run_cells(["p01", "p05"], run_one)
    assert stopped == "cell error at p05 on: CalledProcessError"
    assert list(cells["on"]) == ["p01"] and list(cells["off"]) == ["p01"]


def test_leak_words_numbers_and_curly_apostrophes():
    assert run.leak_words("At 30, then 45 and 100.", "At thirty, then forty-five and one hundred.") == []
    assert run.leak_words("It\u2019s fine.", "It's fine.") == []
    assert run.leak_words("It's fine.", "It\u2019s fine, sighs.") == ["sighs"]
    assert run.number_words(3) == ["three"] and run.number_words(1999) == []


def test_a_wrong_voice_stops_the_ava_run():
    adam = {"item": "p01", "arm": "on", "sent_to_voice": [{"voice": "pNInz6obpgDQGcFmaJgB", "model": "eleven_v4"}]}
    ava = {"item": "p01", "arm": "on", "sent_to_voice": [{"voice": "ava-voice-id", "model": "eleven_v4"}]}
    assert run.voice_mismatch(adam, "ava") == "pNInz6obpgDQGcFmaJgB/eleven_v4"
    assert run.voice_mismatch(ava, "ava") is None and run.voice_mismatch(adam, "fish") is None
    stopped = dict(cell("p01", "on"), voice_mismatch="pNInz6obpgDQGcFmaJgB/eleven_v4")
    assert run.stop_reason(stopped) == "wrong voice at p01 on: pNInz6obpgDQGcFmaJgB/eleven_v4"


def test_published_evidence_names_machine_canaries_by_label():
    canaries = ["/home/someone/repos", "/home/someone/repos/chip-wt", "lane-x"]
    row = json.dumps({"canaries": {c: False for c in canaries}, "note": "under /home/someone/repos/chip-wt/a"})
    public = run.label_canaries(row, canaries)
    assert not any(c in public for c in canaries)
    assert '"machine-canary-2": false' in public and "under machine-canary-2/a" in public
    assert run.label_canaries("no canary here", canaries) == "no canary here"


def test_prepare_records_a_digest_of_the_hermes_source(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "rev", lambda repo, ref: "0" * 40)
    monkeypatch.setattr(run, "archive", lambda repo, ref, dest: dest.mkdir(parents=True))
    host, provider = tmp_path / "host", tmp_path / "provider"
    (host / ".venv").mkdir(parents=True)
    (host / "run_agent.py").write_text("source")
    (host / ".venv" / "site.py").write_text("installed")
    provider.mkdir()
    (provider / "p.py").write_text("provider")
    paths = {"host": host, "host_label": "a label", "fish_repo": tmp_path, "chip_repo": tmp_path, "provider_src": provider}
    shas = run.prepare(tmp_path / "run1", "fake-key", paths)
    assert shas["hermes"] == "a label" and shas["hermes_tree"] == run.tree_sha256(host, skip=run.HOST_SKIP)
    (host / "run_agent.py").write_text("edited source")
    assert run.prepare(tmp_path / "run2", "fake-key", paths)["hermes_tree"] != shas["hermes_tree"]


def test_published_errors_name_configured_roots_and_home_by_label(tmp_path):
    runs = run.HOME / "bench-scratch" / "hermes-runs"
    paths = {"runs_root": runs, "host": run.HOME / ".cache" / "host", "evidence_root": tmp_path / "evidence",
             "eleven_key_file": None, "host_label": "label"}
    roots = run.machine_roots(paths)
    error = f'Traceback: File "{paths["host"]}/.venv/lib/x.py" in {runs}/r1/tools/turn.py; cwd {run.HOME}/elsewhere'
    public = run.label_canaries(error, [str(run.HOME / "src")], roots)
    assert str(run.HOME) not in public and "<host>/.venv/lib/x.py" in public
    assert "<runs_root>/r1/tools/turn.py" in public
    assert "cwd ~/elsewhere" in public
    # A canary longer than the home folder keeps its own label.
    assert run.label_canaries(f"{run.HOME}/src/chip", [str(run.HOME / "src")], roots) == "machine-canary-1/chip"


def test_publish_labels_roots_and_its_leak_scan_covers_them(tmp_path, monkeypatch):
    manifest = {"run_id": "r1", "claim_class": "local_smoke", "proof_boundary": "pb", "does_not_prove": "dnp",
                "shas": {"hermes": "h", "fish_audio": "f", "emotion_chip": "e", "provider_tree": "p",
                         "bench_probe_tree": "b"}, "model": "m", "effort": "medium", "fish_model": "s2.1-pro",
                "arms": {arm: {"cells": 0, "voiced": 0, "isolation_passes": 0, "call1_median": None}
                         for arm in run.ARMS},
                "token_delta": {"tokens": None, "system_chars": None, "system_chars_as_tokens": None}, "cells": [],
                "stopped": f"host error under {run.HOME}/bench-scratch/r1"}
    roots = run.machine_roots({"runs_root": run.HOME / "bench-scratch"})
    (tmp_path / "run").mkdir()
    run.publish(tmp_path / "run", tmp_path / "ev1", manifest, [], {}, {}, ["secret"], roots=roots)
    published = (tmp_path / "ev1" / "manifest.json").read_text()
    assert str(run.HOME) not in published and "host error under <runs_root>/r1" in published
    monkeypatch.setattr(run, "label_canaries", lambda text, canaries, roots=None: text)
    with pytest.raises(SystemExit, match="machine path"):
        run.publish(tmp_path / "run", tmp_path / "ev2", manifest, [], {}, {}, ["secret"], roots=roots)


@pytest.mark.parametrize("run_id", ["../escape", "/abs/run", "a/b", "..", "", ".hidden"])
def test_run_id_must_be_a_plain_name(run_id):
    with pytest.raises(SystemExit, match="plain name"):
        run.main(["--run-id", run_id])


@pytest.mark.parametrize("spec", ["", ",", " , ", "p01,p01"])
def test_custom_item_selection_needs_distinct_items(spec):
    with pytest.raises(SystemExit, match="at least one item"):
        run.select_items(spec)
