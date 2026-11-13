"""M1 fake operating paths and text-only Hermes API contracts."""
import ast
import os
import re
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from types import SimpleNamespace

import pytest
import yaml

from emotion_chip.data import DIALECTS, load_data
from emotion_chip.guide import build_prompt_section
from emotion_chip.hermes import fishtags as fishtags_module
from emotion_chip.hermes.fishtags import adapt_all
from emotion_chip.hermes import provider as provider_module
from emotion_chip.hermes import log as log_module
from emotion_chip.hermes.hooks import HOOKS
from emotion_chip.hermes.registration import model_key, register_all
from emotion_chip.hermes.settings import DEFAULTS, Settings
from emotion_chip.hermes.situation import situation_line
from fakes import fish_like_adapt_tags, FakeContext, FakeFish


def latest():
    """The newest render explanation (last_render is keyed by session)."""
    with provider_module.last_render_lock:
        return list(provider_module.last_render.values())[-1]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ctx, fish = FakeContext(), FakeFish()
    host = {"tts.provider": "emotion-chip", "tts.fish-audio.model": "s2.1-pro"}
    session = {"HERMES_SESSION_ID": "fixture-session", "HERMES_SESSION_PLATFORM": "telegram"}
    now = [10000.0]
    monkeypatch.setattr(provider_module, "_fish", lambda: fish)
    with provider_module.last_render_lock:
        provider_module.last_render.clear()
    register_all(ctx, reader=lambda key, default=None: host.get(key, default),
                 data_dir=lambda: tmp_path / "plugin-data", env=lambda key: session.get(key, ""), clock=lambda: now[0])
    return ctx, fish, host, session, now, tmp_path / "plugin-data"


def test_registration(rig):
    ctx, _, _, _, _, directory = rig
    assert ctx.calls == [("provider", "emotion-chip"), ("section", "emotion-chip"), ("skill", "emotion-chip-report-problem"),
                         ("skill", "emotion-chip-voice"), *[("hook", name) for name in HOOKS],
                         ("middleware", "tool_request"), ("command", "chip"), ("cli_command", "chip")]
    path, description = ctx.skill
    assert path == directory / "skills" / "emotion-chip-voice" / "SKILL.md"
    assert description == path.read_text().split("description: ", 1)[1].splitlines()[0]
    assert ctx.max_chars == 4000
    assert ctx.command_info == ("Hermes Emotion Chip voice delivery", "[status|why|quiet|register]")
    assert all(f"{key}:" in ctx.command("status") for key in
               ("backend", "fish_model", "dialect", "guide_mode", "signals", "quiet", "delivery_log"))


def test_hermes_cli_command(rig, capsys, monkeypatch):
    # The way Hermes attaches a plugin command (hermes_cli/main.py): its own top-level dest="command",
    # a "chip" subparser built by setup_fn, and set_defaults(func=handler_fn); rc = args.func(args).
    import argparse
    import io
    ctx = rig[0]
    help_text, setup_fn, handler_fn, description = ctx.cli_command
    assert help_text.startswith("Hermes Emotion Chip") and "Offline" in description
    top = argparse.ArgumentParser(prog="hermes")
    chip = top.add_subparsers(dest="command").add_parser("chip")
    setup_fn(chip)
    chip.set_defaults(func=handler_fn)
    args = top.parse_args(["chip", "render", "--dialect", "fish_drama3", "[stress: awake] Now."])
    assert args.command == "chip" and args.func(args) == 0
    assert capsys.readouterr().out == "<stress>awake</stress> Now.\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("[pause] Hi."))
    args = top.parse_args(["chip", "render", "--dialect", "eleven_v4"])
    assert args.func(args) == 0 and capsys.readouterr().out == "[pause] Hi.\n"

    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty(""))
    args = top.parse_args(["chip", "render", "--dialect", "eleven_v4"])
    assert args.func(args) == 2 and "give the score as an argument" in capsys.readouterr().err
    args = top.parse_args(["chip", "guide", "--dialect", "eleven_v4", "--model", "claude-opus-5-5"])
    assert args.func(args) == 0 and "Voice delivery" in capsys.readouterr().out


def test_defer(tmp_path):
    ctx = FakeContext({"guide_mode": "defer"})
    directory = tmp_path / "unused"
    register_all(ctx, reader=lambda key, default=None: "emotion-chip", data_dir=lambda: directory)
    assert ctx.section({"model": "default"}) == ""
    # Only the shipped report-problem skill: defer writes and registers no delivery guidance.
    assert [name for kind, name in ctx.calls if kind == "skill"] == ["emotion-chip-report-problem"]
    assert not directory.exists()
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[sad] Hello"}) is None


@pytest.mark.parametrize("mode", ["generate", "defer"])
def test_report_problem_skill_ships_in_every_mode(tmp_path, mode):
    ctx = FakeContext({"guide_mode": mode})
    skills = []
    ctx.register_skill = lambda name, path, description="": skills.append((name, path, description))
    register_all(ctx, reader=lambda key, default=None: "emotion-chip", data_dir=lambda: tmp_path / "plugin-data")
    name, path, description = skills[0]
    text = path.read_text(encoding="utf-8")
    assert name == path.parent.name == "emotion-chip-report-problem" and path.is_file()
    assert path.parent.parent == Path(__file__).resolve().parents[1] / "plugin" / "skills"
    assert f"name: {name}" in text.split("---", 2)[1]
    assert description == "Hermes Emotion Chip: report a bug or problem"
    for link in ("100yenadmin/ai-emotion-chip/security/advisories/new", "issues/new?template=bug_report.yml"):
        assert link in text
    assert [n for n, _, _ in skills] == (["emotion-chip-report-problem", "emotion-chip-voice"] if mode == "generate"
                                          else ["emotion-chip-report-problem"])


def test_unreadable_report_skill_never_stops_registration(tmp_path, monkeypatch, caplog):
    import emotion_chip.hermes.registration as registration
    monkeypatch.setattr(registration, "REPORT_SKILL", tmp_path / "missing" / "SKILL.md")
    ctx = FakeContext({})
    register_all(ctx, reader=lambda key, default=None: "emotion-chip", data_dir=lambda: tmp_path / "plugin-data")
    assert [name for kind, name in ctx.calls if kind == "skill"] == ["emotion-chip-voice"]
    assert ("command", "chip") in ctx.calls and "report-problem skill not registered" in caplog.text


def test_guarded_registrations():
    class Minimal:
        state = {}

        def get_config(self, key, default=None):
            return default
    register_all(Minimal(), reader=lambda key, default=None: default)


def test_root_entry_loads_like_hermes(tmp_path):
    """Load the plugin as Hermes does (hermes_plugins.<slug>, plugin dir not on sys.path) in a clean interpreter."""
    root = Path(__file__).parents[1] / "plugin"
    script = f"""
import importlib.util, sys, types
sys.modules["hermes_plugins"] = types.ModuleType("hermes_plugins"); sys.modules["hermes_plugins"].__path__ = []
spec = importlib.util.spec_from_file_location("hermes_plugins.emotion_chip", {str(root / "__init__.py")!r},
                                              submodule_search_locations=[{str(root)!r}])
entry = importlib.util.module_from_spec(spec); entry.__path__ = [{str(root)!r}]
sys.modules["hermes_plugins.emotion_chip"] = entry; spec.loader.exec_module(entry)
calls = []
class Ctx:
    state = {{}}
    def get_config(self, key, default=None): return default
    def register_tts_provider(self, provider): calls.append(("provider", provider.name))
    def register_hook(self, name, callback): calls.append(("hook", name))
entry.register(Ctx())
assert "emotion_chip" not in sys.modules, "top-level emotion_chip was imported"
print(sorted(set(calls)))
"""
    p = subprocess.run([sys.executable, "-I", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "('provider', 'emotion-chip')" in p.stdout and "('hook', 'pre_llm_call')" in p.stdout


def test_provider_fish_s2_and_drama(rig, runtime_data):
    ctx, fish, host, *_ = rig
    host.pop("tts.fish-audio.model")
    metadata = {}
    result = ctx.provider.synthesize("[slightly sad] Again. [sigh] [pause] Checking.",
                                     "out.mp3", voice="fixture", speed=1.2, result_metadata=metadata)
    kind, text, args = fish.calls[-1]
    assert result == "out.mp3" and kind == "synthesize"
    assert text == "[slightly sad] Again. [sighing] [break] Checking."
    factor = runtime_data.groups[runtime_data.lookup("sad")["group"]]["fish_speed"]
    assert factor != 1.0
    assert args == {"voice": "fixture", "model": None, "speed": 1.2 * factor,
                    "format": "mp3", "result_metadata": metadata}
    assert args["result_metadata"] is metadata
    ctx.set_config("fish_model", "drama-3")
    ctx.provider.synthesize("[soft: here] [speaker: 2]", "out.wav", format="wav")
    assert fish.calls[-1][1] == "<soft>here</soft>" and fish.calls[-1][2]["model"] == "drama-3"
    ctx.provider.synthesize("[soft: here]", "out.mp3", model="s2.1-pro-free")
    assert fish.calls[-1][1] == "[soft tone] here" and fish.calls[-1][2]["model"] == "s2.1-pro-free"
    ctx.provider.synthesize("[soft: here]", "out.mp3", model="not-a-fish-model")
    assert fish.calls[-1][2]["model"] == "drama-3"


@pytest.mark.parametrize("base, expected", [(0, 0.5), (0.1, 0.5), (3, 2.0)])
def test_speed_bounds(rig, base, expected):
    ctx, fish, *_ = rig
    ctx.provider.synthesize("[sad] Again.", "out.mp3", speed=base)
    assert fish.calls[-1][2]["speed"] == expected


def test_quiet_is_the_only_sound_switch(rig):
    ctx, fish, _, session, *_ = rig
    session["HERMES_SESSION_PLATFORM"] = "slack"
    ctx.provider.synthesize("[sad] Hello. [sigh]", "out.mp3")
    assert fish.calls[-1][1] == "[sad] Hello. [sighing]"
    session["HERMES_SESSION_PLATFORM"] = "telegram"
    assert ctx.command("quiet on") == "Sounds quiet: on (every chat on this profile)" and ctx.state["quiet"] is True
    list(ctx.provider.stream("[sigh] [crying] Hello."))
    assert fish.calls[-1][1] == "[crying] Hello."
    assert ctx.command("quiet off") == "Sounds quiet: off (every chat on this profile)"
    ctx.provider.synthesize("[sigh] Hello.", "out.mp3")
    assert fish.calls[-1][1] == "[sighing] Hello."
    assert ctx.command("quiet") == "Sounds quiet: on (every chat on this profile)"


def test_stream_carry_session_and_expiry(rig):
    ctx, fish, _, session, now, _ = rig
    for piece in ("[sad] One.", "Two.", "Three."):
        assert list(ctx.provider.stream(piece)) == [b"pcm"]
        now[0] += 1
    assert [row[2]["speed"] for row in fish.calls] == [0.92] * 3
    session["HERMES_SESSION_ID"] = "other-fixture-session"
    list(ctx.provider.stream("Four."))
    assert fish.calls[-1][2]["speed"] == 1.0
    session["HERMES_SESSION_ID"] = "fixture-session"
    now[0] += 121
    list(ctx.provider.stream("Five."))
    assert fish.calls[-1][2]["speed"] == 1.0
    list(ctx.provider.stream("[curious] Six.", speed=1.5))
    assert fish.calls[-1][2]["speed"] == 1.5 * 1.03
    list(ctx.provider.stream("Seven."))
    assert fish.calls[-1][2]["speed"] == 1.03


@pytest.mark.parametrize("gateway", [False, True])
def test_new_turn_clears_stream_carry(rig, gateway):
    ctx, fish, _, session, _, _ = rig
    session.update(HERMES_SESSION_ID="" if gateway else "session-a", HERMES_SESSION_KEY="chat-a")
    ctx.hooks["pre_llm_call"](session_id="session-a", turn_id="turn-1")
    list(ctx.provider.stream("[[register: sad]] First reply."))
    assert fish.calls[-1][2]["speed"] == 0.92
    parent = dict(ctx.state["sessions"]["session-a"])
    for child in ({"parent_session_id": "session-a"}, {"platform": "subagent"}):
        ctx.hooks["pre_llm_call"](session_id="child-a", **child)
        list(ctx.provider.stream("Another piece of the first reply."))
        assert fish.calls[-1][2]["speed"] == 0.92
    assert ctx.state["session_keys"] == {"chat-a": "session-a"}
    assert ctx.state["sessions"]["session-a"] == parent
    ctx.hooks["pre_llm_call"](session_id="session-a", turn_id="turn-2")
    list(ctx.provider.stream("A fresh neutral reply."))
    assert fish.calls[-1][2]["speed"] == 1.0


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("speed", [None, 1.1, 3.0])
def test_why_and_log_use_final_fish_speed(rig, streaming, speed):
    ctx, fish, host, _, _, directory = rig
    host["tts.fish-audio.speed"] = 1.3
    if streaming:
        list(ctx.provider.stream("[sad] Again.", speed=speed))
    else:
        ctx.provider.synthesize("[sad] Again.", "out.mp3", speed=speed)
    expected = max(0.5, min(2.0, (1.3 if speed is None else speed) * 0.92))
    sent = fish.calls[-1][2]["speed"]
    assert sent == expected
    assert f"params: {{'speed': {sent}}}" in ctx.command("why")
    assert json.loads((directory / "delivery.jsonl").read_text())["params"] == {"speed": sent}


def test_stream_concurrent_carry(rig, monkeypatch):
    ctx, fish, *_ = rig
    original = fishtags_module.render

    def checked_render(*args, **kwargs):
        assert ctx.provider._lock._is_owned(), "carry read/render/write must hold its lock"
        return original(*args, **kwargs)
    monkeypatch.setattr(fishtags_module, "render", checked_render)
    list(ctx.provider.stream("[sad] First."))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda i: list(ctx.provider.stream(f"Piece {i}.")), range(9)))
    assert all(row[2]["speed"] == 0.92 for row in fish.calls)


def test_stream_carries_last_provider_group(rig):
    ctx, fish, *_ = rig
    ctx.provider.synthesize("[sad] First.", "out.mp3")
    list(ctx.provider.stream("Second."))
    assert fish.calls[-1][2]["speed"] == 0.92


def test_fish_capabilities_and_delegation(rig, monkeypatch):
    ctx, fish, *_ = rig
    p = ctx.provider
    assert p.display_name == "Hermes Emotion Chip" and p.voice_compatible
    assert p.streams_pcm and p.stream_sample_rate == 24000 and p.is_available()
    fish.streams_pcm, fish.stream_sample_rate, fish.available = False, 16000, False
    assert not p.streams_pcm and p.stream_sample_rate == 16000 and not p.is_available()
    assert p.list_voices() == fish.list_voices() and p.default_voice() == fish.default_voice()
    assert p.list_models() == fish.list_models()
    p.warm()
    p.release()
    assert fish.calls == [("warm",), ("release",)]
    monkeypatch.setattr(provider_module, "_fish", lambda: None)
    assert not p.streams_pcm and p.stream_sample_rate is None and not p.is_available()
    assert p.list_voices() == [] and p.default_voice() is None and p.list_models() == []
    assert p.warm() is None and p.release() is None
    for call in (lambda: p.synthesize("Hi.", "out.mp3"), lambda: p.stream("Hi.")):
        with pytest.raises(ValueError, match=r"^Hermes Emotion Chip needs the fish-audio plugin enabled\.$"):
            call()


@pytest.mark.parametrize("provider, model, expected", [
    ("fish-audio", "drama-3", "<soft>here</soft> [sighing]"),
    ("elevenlabs", "eleven_v4", "[softly] here [sighs]"),
    ("elevenlabs", "eleven_v3_conversational", "[softly] here [sighs]"),
    ("elevenlabs", "eleven_multilingual_v2", "here"), ("edge", "", "here"),
])
def test_middleware_dialects(rig, provider, model, expected):
    ctx, _, host, *_ = rig
    host.update({"tts.provider": provider, "tts.fish-audio.model": model, "tts.elevenlabs.model_id": model})
    args = {"text": "[soft: here] [sigh]", "output_path": "fixture.mp3"}
    assert ctx.middleware(tool_name="text_to_speech", args=args, future_key=1) == {
        "args": {"text": expected, "output_path": "fixture.mp3"}}
    assert args["text"] == "[soft: here] [sigh]"
    assert ("<soft>" in expected) == (provider == "fish-audio" and model == "drama-3")


def test_middleware_default_provider_keeps_code_inside_a_span(rig):
    ctx, _, host, *_ = rig
    host.pop("tts.provider")
    args = {"text": "[whisper: call items[index] now]", "output_path": "fixture.mp3"}
    assert ctx.middleware(tool_name="text_to_speech", args=args) == {
        "args": {"text": "call items[index] now", "output_path": "fixture.mp3"}}
    assert args["text"] == "[whisper: call items[index] now]"


def test_span_code_placeholders_do_not_reach_why(rig):
    ctx, fish, *_ = rig
    ctx.provider.synthesize("[whisper: call items[index] now]", "fixture.mp3")
    assert fish.calls[-1][1] == "[whispering] call items[index] now"
    why = ctx.command("why")
    assert "\ue000" not in why and "\ue001" not in why


# Hermes runs a command provider for a non-built-in name whose config (tts.providers.<name>, else tts.<name>) has a
# non-empty command and an empty or "command" type; the chip leaves that text alone and renders everything else.
@pytest.mark.parametrize("provider, config, untouched", [
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": "command", "command": "say"}}}, True),
    ("SaG-EvA", {"tts.providers": {"sag-eva": {"type": "command", "command": "say"}}}, True),
    ("sag-eva", {"tts.providers": {"sag-eva": {"command": "say"}}}, True),
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": "", "command": "say"}}}, True),
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": " Command ", "command": "say"}}}, True),
    ("sag-eva", {"tts": {"sag-eva": {"command": "say"}}}, True),
    ("my.tool", {"tts.providers": {"my.tool": {"command": "say"}}}, True),
    ("my.tool", {"tts": {"my.tool": {"command": "say"}}}, True),
    ("edge", {"tts.providers": {"edge": {"type": "command", "command": "say"}}}, False),
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": "command"}}}, False),
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": "command", "command": "  "}}}, False),
    ("sag-eva", {"tts.providers": {"sag-eva": {"type": "plugin", "command": "say"}}}, False),
    (None, {"tts.providers": {"none": {"command": "say"}}}, False),
    ("", {"tts.providers": {"": {"command": "say"}}}, False),
])
def test_middleware_leaves_command_providers_alone(rig, provider, config, untouched):
    ctx, _, host, *_ = rig
    host.update({"tts.provider": provider, **config})
    result = ctx.middleware(tool_name="text_to_speech", args={"text": "[whispers] [crying] Hi. [laughs]"})
    assert result is None if untouched else result == {"args": {"text": "Hi."}}


def test_middleware_noops_and_reader_error(rig):
    ctx, _, host, *_ = rig
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[sad] Hi."}) is None
    host["tts.provider"] = "edge"
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "Hi."}) is None
    assert ctx.middleware(tool_name="other", args={"text": "[sad] Hi."}) is None
    assert ctx.middleware(tool_name="text_to_speech", args={"text": None}) is None

    def broken(*args):
        raise RuntimeError("fixture reader error")
    ctx.provider.settings.reader = broken
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[sad] Hi."}) is None


def test_signals(rig):
    ctx, _, _, _, now, _ = rig
    hooks, kw = ctx.hooks, {"session_id": "fixture-session", "future_key": "ignored"}
    assert hooks["pre_llm_call"](**kw) is None
    hooks["on_session_start"](**kw)
    assert hooks["pre_llm_call"](**kw) is None
    for _ in range(3):
        hooks["post_tool_call"](**kw, status="error")
    hooks["post_tool_call"](**kw, status="blocked")
    hooks["api_request_error"](**kw, retry_count=2)
    hooks["api_request_error"](**kw, retry_count=1)
    hooks["on_session_end"](**kw, interrupted=False)
    hooks["on_session_end"](**kw, interrupted=True)
    row = ctx.state["sessions"]["fixture-session"]
    assert row["tool_error_streak"] == 3 and row["successes_since_error"] == 0
    assert row["last_error_at"] == now[0] and row["transport_errors"] == 2 and row["retries"] == 2
    assert row["interruptions"] == 1 and row["started_at"] == now[0]
    assert hooks["pre_llm_call"](**kw) is None
    ctx.set_config("signals", "facts")
    now[0] += 6000
    line = hooks["pre_llm_call"](**kw, sender_id="fixture-sender", platform="telegram")["context"]
    assert len(line) <= 200 and "3 tool errors in a row" in line and "session 1 h 40 min" in line
    assert "Stopping and reporting is fine." not in line
    for word in ("frustrated", "sad", "angry", "upset", "happy", "anxious", "stressed", "worried"):
        assert word not in line.lower()
    ctx.set_config("signals", "facts+affordance")
    line = hooks["pre_llm_call"](**kw)["context"]
    assert len(line) <= 200 and line.endswith(" Stopping and reporting is fine.")
    hooks["post_tool_call"](**kw, status="ok")
    row = ctx.state["sessions"]["fixture-session"]
    assert row["tool_error_streak"] == 0 and row["successes_since_error"] == 1 and row["transport_errors"] == 2
    assert row["sender_id"] == "fixture-sender" and row["platform"] == "telegram"
    assert "Situation at" in ctx.command("why")
    ctx.set_config("signals", "off")
    assert hooks["pre_llm_call"](**kw) is None
    assert line in ctx.command("why")


def test_facts_without_counters(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    ctx.hooks["on_session_start"](session_id="fixture-session")
    assert ctx.hooks["pre_llm_call"](session_id="fixture-session") is None


def test_json_state_facade(rig):
    ctx, *_ = rig

    class State:
        def __init__(self):
            self.values = {}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value
    ctx.provider.situation.state = state = State()
    ctx.hooks["post_tool_call"](session_id="fixture-session", status="error")
    assert state.values["sessions"]["fixture-session"]["tool_error_streak"] == 1
    ctx.command("quiet on")
    assert state.values["quiet"] is True


def test_signals_prune_and_large_counts(rig):
    ctx, _, _, _, now, _ = rig
    for i in range(55):
        now[0] += 1
        ctx.hooks["on_session_start"](session_id=f"fixture-{i}")
    assert len(ctx.state["sessions"]) == 50
    assert "fixture-0" not in ctx.state["sessions"] and "fixture-54" in ctx.state["sessions"]
    row = {k: 10**20 for k in ("tool_error_streak", "successes_since_error", "transport_errors", "retries", "interruptions")}
    row["started_at"] = 0
    line = situation_line(row, now[0], True)
    assert len(line) <= 200
    assert f"{10**20} tool errors in a row" in line and "999999" not in line


def test_delivery_log_and_rotation(rig, monkeypatch):
    ctx, _, _, _, _, directory = rig
    ctx.hooks["pre_llm_call"](session_id="fixture-session", sender_id="fixture-sender", platform="telegram")
    text = "[sad] Private fixture phrase. [sigh]"
    ctx.provider.synthesize(text, "out.mp3")
    path = directory / "delivery.jsonl"
    raw, row = path.read_text(), json.loads(path.read_text())
    assert set(row) == {"ts", "session_id", "sender_id", "platform", "dialect", "cue_words", "group", "params", "dropped_kinds", "chars", "fish_model"}
    assert "Private fixture phrase" not in raw and text not in raw and row["chars"] == len(text)
    assert row["sender_id"] == "fixture-sender" and row["platform"] == "telegram"
    assert row["cue_words"] == ["sad"] and row["group"] == "sadness" and row["params"] == {"speed": 0.92}
    assert log_module.MAX_BYTES == 5 * 1024 * 1024
    monkeypatch.setattr(log_module, "MAX_BYTES", len(raw.encode()) + 1)
    list(ctx.provider.stream("Two."))
    assert path.with_name("delivery.1.jsonl").read_text() == raw
    assert len(path.read_text().splitlines()) == 1
    ctx.set_config("delivery_log", False)
    before = path.read_bytes()
    ctx.provider.synthesize("Three.", "out.mp3")
    assert path.read_bytes() == before


def test_delivery_log_failure_never_stops_speech(rig, caplog):
    ctx, fish, _, _, _, directory = rig
    (directory / "delivery.jsonl").mkdir(parents=True)  # a folder where the log file should be: every write fails
    for _ in range(2):
        assert ctx.provider.synthesize("[sad] Still spoken.", "out.mp3") == "out.mp3"
        assert list(ctx.provider.stream("[sad] Streamed too.")) == [b"pcm"]
    assert [call[0] for call in fish.calls] == ["synthesize", "stream"] * 2
    assert caplog.text.count("delivery log not written") == 1
    (directory / "delivery.jsonl").rmdir()  # writable again: later rows are written, not suppressed for good
    ctx.provider.synthesize("[sad] Logged again.", "out.mp3")
    assert json.loads((directory / "delivery.jsonl").read_text())["cue_words"] == ["sad"]


def test_delivery_log_off_creates_no_file(rig):
    ctx, _, _, _, _, directory = rig
    ctx.set_config("delivery_log", False)
    ctx.provider.synthesize("No log.", "out.mp3")
    list(ctx.provider.stream("Still no log."))
    assert not (directory / "delivery.jsonl").exists()


def test_commands_why_and_register(rig, runtime_data):
    ctx, *_ = rig
    assert ctx.command("register") == "No authored register recorded."
    ctx.provider.synthesize("[[register: calm]] [curious] One. [sigh] [laugh] [speaker: 2]", "out.mp3")
    why = ctx.command("why")
    assert "fish_s2" in why and "curious" in why and "calm" in why and "params:" in why
    authored = {"calm", "curious", "sigh", "laugh"}
    for word in set(runtime_data.groups) | {r["id"] for r in runtime_data.emotions}:
        if word not in authored:
            assert not re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", why)
    assert "speaker 2: unsupported" in why and "laugh:" not in why and "caps" not in why
    for unauthed in ("sad", "frustrated", "angry", "happy", "anxious", "bored"):
        assert unauthed not in why
    assert ctx.command("register") == "[[register: calm]]"
    ctx.command("register clear")
    assert ctx.command("register") == "No authored register recorded."
    assert ctx.command("invalid").startswith("Usage: /chip")


@pytest.mark.parametrize("name, expected", [("anthropic/claude-opus-5-5-20261001", "claude-opus-5-5"),
    ("claude-fable-5-1-fast", "claude-fable-5-1"), ("other", "default"), (None, "default")])
def test_model_key(name, expected):
    assert model_key(name) == expected


def test_prompt_sections(rig, runtime_data):
    ctx, _, host, *_ = rig
    for dialect in DIALECTS:
        for key in runtime_data.models:
            section = build_prompt_section(dialect, key, runtime_data)
            assert len(section) <= 3800 and "When you call the text_to_speech tool, write the score in its text." in section
    assert ctx.section({"model": "provider/claude-opus-5-5-fast"})
    host["tts.provider"] = "edge"
    assert ctx.section({"model": "default"}) == ""
    host["tts.provider"] = "emotion-chip"
    ctx.set_config("guide_mode", "defer")
    assert ctx.section({"model": "default"}) == ""


def test_settings_defaults(rig):
    ctx, _, host, *_ = rig
    settings = Settings(ctx, lambda key, default=None: host.get(key, default))
    assert {key: settings.get(key) for key in DEFAULTS} == DEFAULTS
    assert settings.host_fish_model() == "s2.1-pro"


@pytest.mark.parametrize("raw, expected", [
    ("Fish", "fish"), (" ELEVEN ", "eleven"), ("eleven\n", "eleven"),
    (None, "fish"), (3, "fish"), ([], "fish"), ("elevenlabs", "fish"), ("", "fish"),
])
def test_backend_setting_normalization(raw, expected):
    assert Settings(FakeContext({"backend": raw})).get("backend") == expected


@pytest.mark.parametrize("config", [{}, {"backend": "fish"}, {"backend": "eleven"},
                                    {"backend": "Fish"}, {"backend": " ELEVEN "}, {"backend": "eleven\n"}])
def test_backend_problem_valid_settings(config):
    assert Settings(FakeContext(config)).backend_problem() is None
    assert Settings().backend_problem() is None


@pytest.mark.parametrize("raw", ["elevenlabs", "", None, 3, []])
def test_backend_problem_invalid_settings(raw):
    assert Settings(FakeContext({"backend": raw})).backend_problem() == (
        f"backend is {raw!r}; set backend: fish or eleven (using fish until then)")


@pytest.mark.parametrize("raw", ["bad\nvalue", "bad\r\t\x00\x1bvalue", "bad; backend: eleven", "x" * 100])
def test_backend_problem_safe_status_value(raw):
    problem = Settings(FakeContext({"backend": raw})).backend_problem()
    shown = problem.removeprefix("backend is ").removesuffix(
        "; set backend: fish or eleven (using fish until then)")
    assert len(shown) <= 40 and all(char.isprintable() for char in shown)
    assert ";" not in shown
    assert len(problem.splitlines()) == 1
    if "\n" in raw:
        assert r"\n" in shown


@pytest.mark.parametrize("raw", ["Fish", "elevenlabs"])
def test_fish_backend_normalization_preparation_and_synthesis(rig, raw):
    ctx, fish, host, *_ = rig
    ctx.provider.tag_adapter = fish_like_adapt_tags
    ctx.set_config("fish_model", "drama-3")
    fish.pinned_model = host["tts.fish-audio.model"]
    text = "(sad) [soft: Again.] [sigh]"
    ctx.set_config("backend", "fish")
    canonical = ctx.provider._prepare(text, None, None)
    models = ctx.provider._resolve_model()
    explanation = dict(latest())
    ctx.provider.synthesize(text, "out.mp3")
    canonical_call = fish.calls[-1]
    list(ctx.provider.stream(text))
    canonical_stream = fish.calls[-1]

    ctx.set_config("backend", raw)
    assert ctx.provider._prepare(text, None, None) == canonical
    assert ctx.provider._resolve_model() == models
    assert latest() == explanation  # Includes dialect, params, notes and Fish model.
    assert ctx.provider.synthesize(text, "out.mp3") == "out.mp3"
    assert fish.calls[-1] == canonical_call
    assert list(ctx.provider.stream(text)) == [b"pcm"]
    assert fish.calls[-1] == canonical_stream
    assert fish.effective_models[-1] == "s2.1-pro"


def test_eleven_backend_normalization_preparation_and_synthesis(rig, monkeypatch):
    from emotion_chip.hermes import eleven as eleven_module
    from test_eleven import Response, payload
    ctx, fish, _, _, _, directory = rig
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response()

    ctx.provider.eleven.opener = opener
    monkeypatch.setattr(eleven_module, "eleven_api_key", lambda: "fixture-eleven-key")
    monkeypatch.setattr(provider_module, "_fish", lambda: pytest.fail("Eleven must not resolve Fish"))
    ctx.settings.update(backend="eleven", eleven_voice_id="setting-voice", eleven_style=0.3, eleven_speed=0.9)
    text = "[sad] [soft: Again.] [sigh]"
    canonical = ctx.provider._prepare(text, None, None)
    ctx.set_config("backend", " Eleven ")
    assert ctx.provider._prepare(text, None, None) == canonical
    rendered, model, speed = canonical
    assert rendered.dialect == "eleven_v4" and model == "eleven_v4" and speed is None
    output = directory / "out.mp3"
    assert ctx.provider.synthesize(text, output) == output
    expected = {"text": rendered.text, "model_id": model, "voice_settings": rendered.params}
    assert payload(calls) == expected and output.read_bytes() == b"\x01\x00" * 5000
    assert b"".join(ctx.provider.stream(text)) == b"\x01\x00" * 5000
    assert payload(calls) == expected and not fish.calls


@pytest.mark.parametrize("fish_state", ["ready", "missing", "not_ready", "unknown_key", "raises"])
def test_invalid_backend_status_preserves_fish_checks(rig, monkeypatch, fish_state):
    from emotion_chip.hermes import commands
    ctx, fish, *_ = rig
    monkeypatch.setattr(commands, "_has_secret", lambda name: None if fish_state == "unknown_key" else True)
    if fish_state == "missing":
        monkeypatch.setattr(provider_module, "_fish", lambda: None)
    elif fish_state == "not_ready":
        fish.available = False
    elif fish_state == "raises":
        def boom():
            raise RuntimeError("provider bug")
        fish.is_available = boom
    settings = ctx.provider.settings
    problems, unchecked = commands.readiness(settings)
    ctx.set_config("backend", "elevenlabs")
    problem = "backend is 'elevenlabs'; set backend: fish or eleven (using fish until then)"
    assert commands.readiness(settings) == ([problem, *problems], unchecked)
    status = ctx.command("status")
    assert status.startswith("ready: no (") and problem in status and "; backend: fish;" in status
    facts = json.loads(ctx.tools["chip_status"]["handler"]({}))
    assert facts["ready"] == "no" and facts["backend"] == "fish"
    assert facts["problems"] == [problem, *problems] and facts["not_checked"] == unchecked


def test_invalid_backend_does_not_affect_other_provider(rig, monkeypatch):
    from emotion_chip.hermes.commands import readiness
    ctx, _, host, *_ = rig
    host["tts.provider"] = "edge"
    ctx.set_config("backend", "elevenlabs")
    monkeypatch.setattr(provider_module, "_fish", lambda: pytest.fail("Another provider must not check Fish"))
    assert readiness(ctx.provider.settings) == (
        ["the speech provider is edge; set tts.provider: emotion-chip"], [])


def _literal_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing host contract {name}")


def test_hermes_source_contract():
    # A Hermes Agent source checkout: HERMES_AGENT_SRC, else the default install location.
    root = Path(os.environ.get("HERMES_AGENT_SRC") or Path.home() / ".hermes" / "hermes-agent")
    if not root.is_dir():
        pytest.skip("Hermes source absent; text-only local 0.22.2 contract check unavailable in CI")
    plugins = ast.parse((root / "hermes_cli" / "plugins.py").read_text())
    assert set(HOOKS) <= _literal_assignment(plugins, "VALID_HOOKS")
    ctx = next(n for n in plugins.body if isinstance(n, ast.ClassDef) and n.name == "PluginContext")
    methods = {n.name: n for n in ctx.body if isinstance(n, ast.FunctionDef)}
    for name, positional, keywords in (
        ("register_system_prompt_section", ["id", "content"], ["max_chars"]),
        ("register_middleware", ["kind", "callback"], []),
        ("register_command", ["name", "handler"], ["description", "args_hint"]),
        ("register_cli_command", ["name", "help", "setup_fn", "handler_fn"], ["description"]),
        ("register_hook", ["hook_name", "callback"], []), ("get_config", ["key", "default"], []),
    ):
        args = methods[name].args
        assert [a.arg for a in args.args[1:1+len(positional)]] == positional
        assert set(keywords) <= {a.arg for a in [*args.args, *args.kwonlyargs]}
    # Hermes 0.23.0 moved the provider registrar table and its factory into plugins_registrars.py.
    registrars = root / "hermes_cli" / "plugins_registrars.py"
    table = ast.parse(registrars.read_text()) if registrars.is_file() else plugins
    rows = _literal_assignment(table, "_SCOPED_PROVIDER_REGISTRARS")
    tts = next(row for row in rows if row[0] == "register_tts_provider")
    assert tts[2:4] == ("agent.tts_registry", "agent.tts_provider:TTSProvider")
    factory = next(n for n in table.body if isinstance(n, ast.FunctionDef)
                   and n.name in ("_make_scoped_provider_registrar", "make_scoped_provider_registrar"))
    registrar = next(n for n in factory.body if isinstance(n, ast.FunctionDef) and n.name == "register")
    assert [a.arg for a in registrar.args.args] == ["self", "provider"]
    content = ast.parse((root / "hermes_cli" / "plugins_content.py").read_text())
    skill = next(n for n in content.body if isinstance(n, ast.FunctionDef) and n.name == "register_skill")
    assert [a.arg for a in skill.args.args][:4] == ["self", "name", "path", "description"]
    manifest_source = ast.parse((root / "hermes_cli" / "plugins_manifest.py").read_text())
    known = _literal_assignment(manifest_source, "_KNOWN_MANIFEST_FIELDS")
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin" / "plugin.yaml").read_text())
    # Hermes 0.22.2 parser omits provides_middleware, although its validator requires it.
    assert set(manifest) - known == {"provides_middleware"}
    assert "requires_plugins" not in manifest
    assert manifest["provides_middleware"] == ["tool_request"]
    validator = ast.parse((root / "hermes_cli" / "plugin_validate.py").read_text())
    assert any(isinstance(n, ast.Tuple) and len(n.elts) == 2
               and all(isinstance(v, ast.Constant) for v in n.elts)
               and ast.literal_eval(n) == ("middleware", "provides_middleware") for n in ast.walk(validator))
    assert set(manifest["provides_hooks"]) == set(HOOKS)
    assert manifest["manifest_version"] == 2 and manifest["requires_hermes"] == ">=0.22.2"
    assert {key: row["default"] for key, row in manifest["config_schema"].items()} == DEFAULTS
    assert not any(name in sys.modules for name in ("hermes_cli.plugins", "agent.tts_provider"))


@pytest.mark.parametrize("pinned,requested,dialect,text", [
    ("s2.1-pro", "drama-3", "fish_s2", "[soft tone] here"),
    ("drama-3", "s2.1-pro", "fish_drama3", "<soft>here</soft>"),
    ("drama-3-preview", "s2.1-pro", "fish_drama3", "<soft>here</soft>"),
])
def test_pinned_model_all_consumers(rig, pinned, requested, dialect, text):
    ctx, fish, host, _, _, directory = rig
    host["tts.fish-audio.model"] = pinned
    fish.pinned_model = "drama-3" if pinned == "drama-3-preview" else pinned
    ctx.set_config("fish_model", requested)
    ctx.provider.synthesize("[soft: here]", "out.mp3")
    list(ctx.provider.stream("[soft: here]"))
    assert all(call[1] == text and call[2]["model"] == requested for call in fish.calls)
    assert fish.effective_models == [fish.pinned_model] * 2
    row = latest()
    assert row["fish_model"] == fish.pinned_model and row["dialect"] == dialect
    assert any("pinned model wins" in note for note in row["notes"])
    assert f"fish_model: {fish.pinned_model}" in ctx.command("status")
    assert f"Dialect: {dialect}" in ctx.command("why")
    assert fish.pinned_model in ctx.command("why")
    assert ctx.section({"model": "default"}) == build_prompt_section(dialect, "default", load_data())
    logged = json.loads((directory / "delivery.jsonl").read_text().splitlines()[-1])
    assert logged["fish_model"] == fish.pinned_model and logged["dialect"] == dialect
    host["tts.provider"] = "fish-audio"
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[soft: here]"})["args"]["text"] == text


@pytest.mark.parametrize("key", ["tts.fish-audio.model", "tts.model"])
def test_load_time_skill_uses_the_fish_model_hermes_sends(tmp_path, key):
    from emotion_chip.guide import build_skill
    ctx = FakeContext({"fish_model": "s2.1-pro"})
    host = {key: "drama-3", "tts.provider": "emotion-chip"}
    register_all(ctx, reader=lambda name, default=None: host.get(name, default), data_dir=lambda: tmp_path)
    assert build_skill("fish_drama3", "default", load_data()) != build_skill("fish_s2", "default", load_data())
    assert ctx.skill[0].read_text() == build_skill("fish_drama3", "default", load_data()) + "\n"


@pytest.mark.parametrize("configured,call,expected", [
    (None, None, None), ("invalid", "invalid", None), ("drama-3", None, "drama-3"),
    ("fish-default", None, None), ("s2.1-pro", None, "s2.1-pro"),
    ("drama-3", "s2-pro", "s2-pro"), ("drama-3-preview", None, "drama-3"),
])
def test_requested_model_and_wallet_default(rig, configured, call, expected):
    ctx, fish, host, _, _, directory = rig
    host["tts.fish-audio.model"] = "invalid"
    if configured is not None:
        ctx.set_config("fish_model", configured)
    ctx.provider.synthesize("Hello.", "out.mp3", model=call)
    assert fish.calls[-1][2]["model"] == expected
    label = expected or "fish-default"
    assert latest()["fish_model"] == label
    assert f"Fish model: {label}" in ctx.command("why")
    assert json.loads((directory / "delivery.jsonl").read_text().splitlines()[-1])["fish_model"] == label
    if call is None:
        assert f"fish_model: {label}" in ctx.command("status")
    host["tts.fish-audio.model"] = "drama-3"
    assert "fish_model: drama-3" in ctx.command("status")


def test_session_reset_moves_the_key_to_the_new_session(rig):
    ctx, _, _, env, *_ = rig
    env.clear()
    env["HERMES_SESSION_KEY"] = "chat-a"
    ctx.hooks["pre_llm_call"](session_id="A")
    ctx.provider.synthesize("Hello.", "out.mp3")
    reset = ctx.hooks["on_session_reset"]
    for extra in ({"parent_session_id": "A"}, {"platform": "subagent"}):
        reset(session_id="B", old_session_id="A", **extra)
        assert "B" not in ctx.state["sessions"]
    reset(session_id="B", old_session_id="A", new_session_id="B", reason="new_session")
    assert "session_id: B" in ctx.command("status")
    assert ctx.command("why").startswith("No render recorded")
    before = json.dumps(ctx.state, sort_keys=True)
    reset(session_id="C", old_session_id="unmapped", new_session_id="C")
    assert json.dumps(ctx.state, sort_keys=True) == before


def test_session_reset_moves_only_the_chat_that_ran_new(rig):
    ctx, _, _, env, *_ = rig
    env.clear()
    for key in ("chat-a", "chat-b"):
        env["HERMES_SESSION_KEY"] = key
        ctx.hooks["pre_llm_call"](session_id="X")
    reset = ctx.hooks["on_session_reset"]
    env.clear()
    # Two chats share session X (/resume from a second chat) and Hermes names no key: leave both alone.
    reset(session_id="Y", old_session_id="X", new_session_id="Y")
    assert ctx.state["session_keys"] == {"chat-a": "X", "chat-b": "X"}
    env["HERMES_SESSION_KEY"] = "chat-a"
    reset(session_id="Y", old_session_id="X", new_session_id="Y")
    assert ctx.state["session_keys"] == {"chat-a": "Y", "chat-b": "X"}
    # A bound key that is not on the old session never moves another chat's key.
    env["HERMES_SESSION_KEY"] = "chat-a"
    reset(session_id="Z", old_session_id="X", new_session_id="Z")
    assert ctx.state["session_keys"] == {"chat-a": "Y", "chat-b": "X"}


def test_session_reset_keeps_the_key_at_the_row_cap(rig):
    ctx, _, _, env, now, _ = rig
    env.clear()
    env["HERMES_SESSION_KEY"] = "chat-a"
    ctx.hooks["pre_llm_call"](session_id="old")
    env.clear()
    for n in range(49):
        now[0] += 1
        ctx.hooks["post_tool_call"](session_id=f"sub{n}", status="ok")
    now[0] += 1
    ctx.hooks["on_session_reset"](session_id="new", old_session_id="old", new_session_id="new")
    assert ctx.state["session_keys"] == {"chat-a": "new"} and "old" not in ctx.state["sessions"]


@pytest.mark.parametrize("provider", ["emotion-chip", "edge"])
def test_skill_follows_a_live_fish_pin_change(rig, provider):
    from emotion_chip.guide import build_skill
    ctx, _, host, *_ = rig
    host["tts.provider"] = provider
    host["tts.fish-audio.model"] = "drama-3"
    ctx.section({"model": "default"})
    path = ctx.skill[0]
    # On the compat path (#51) the skill teaches the dialect the middleware uses, which a Fish pin does not change.
    text = build_skill("fish_drama3" if provider == "emotion-chip" else "tagless", "default", load_data()) + "\n"
    assert path.read_text() == text
    stamp = path.stat().st_mtime_ns
    ctx.section({"model": "default"})
    assert path.stat().st_mtime_ns == stamp and path.read_text() == text


def test_skill_rewrite_keeps_an_ordinary_file_mode(rig):
    ctx, _, host, *_ = rig
    path = ctx.skill[0]
    path.chmod(0o644)
    host["tts.fish-audio.model"] = "drama-3"
    ctx.section({"model": "default"})
    assert path.stat().st_mode & 0o777 == 0o644 and len(list(path.parent.iterdir())) == 1


def test_skill_rewrite_error_keeps_prompt_working(rig, monkeypatch, caplog):
    ctx, _, host, *_ = rig
    before = ctx.skill[0].read_text()
    host["tts.fish-audio.model"] = "drama-3"
    def fail(*args):
        raise OSError("fixture write failure")
    monkeypatch.setattr("emotion_chip.hermes.registration.os.replace", fail)
    assert ctx.section({"model": "default"}) == build_prompt_section("fish_drama3", "default", load_data())
    assert "skill rewrite failed" in caplog.text and ctx.skill[0].read_text() == before


@pytest.mark.parametrize("host_speed,call,expected", [(1.3, None, 1.196), (1.3, 1.1, 1.012),
                                                       ("1.3", None, .92), (False, None, .92)])
def test_speed_host_base(rig, host_speed, call, expected):
    ctx, fish, host, *_ = rig
    host["tts.fish-audio.speed"] = host_speed
    ctx.provider.synthesize("[sad] Again.", "out.mp3", speed=call)
    assert fish.calls[-1][2]["speed"] == pytest.approx(expected)
    list(ctx.provider.stream("[sad] Again.", speed=call))
    assert fish.calls[-1][2]["speed"] == pytest.approx(expected)


def test_tagless_speed(rig):
    ctx, fish, host, *_ = rig
    host["tts.fish-audio.model"] = "s1"
    ctx.provider.synthesize("[sad] Again.", "out.mp3")
    assert fish.calls[-1][1] == "Again." and fish.calls[-1][2]["speed"] == 1.0


@pytest.mark.parametrize("hook", ["pre_llm_call", "on_session_start", None])
def test_gateway_session_identity_and_carry(rig, hook):
    ctx, fish, _, env, _, directory = rig
    env.update(HERMES_SESSION_ID="", HERMES_SESSION_KEY="telegram:1")
    if hook:
        ctx.hooks[hook](session_id="real-session", sender_id="fixture-sender")
    identity = "real-session" if hook else "key:telegram:1"
    list(ctx.provider.stream("[[register: sad]] One."))
    list(ctx.provider.stream("Two."))
    assert fish.calls[-1][2]["speed"] == .92
    assert latest()["session_id"] == identity
    assert "Dialect:" in ctx.command("why")
    assert identity in ctx.command("status")
    assert ctx.command("register") == "[[register: sad]]"
    assert json.loads((directory / "delivery.jsonl").read_text().splitlines()[0])["session_id"] == identity
    env["HERMES_SESSION_KEY"] = "telegram:2"
    assert ctx.command("why") == "No render recorded for this session."
    assert ctx.command("register") == "No authored register recorded."
    list(ctx.provider.stream("Other."))
    assert fish.calls[-1][2]["speed"] == 1.0
    env["HERMES_SESSION_KEY"] = "telegram:1"
    assert "Other" not in ctx.command("why")
    assert ctx.command("register") == "[[register: sad]]"


def test_session_key_map_pruned_and_id_priority(rig):
    ctx, _, _, env, now, _ = rig
    env["HERMES_SESSION_ID"] = ""
    for i in range(55):
        env["HERMES_SESSION_KEY"] = f"telegram:{i}"
        now[0] += 1
        ctx.hooks["pre_llm_call"](session_id=f"session-{i}")
    assert len(ctx.state["session_keys"]) == 50
    assert "telegram:0" not in ctx.state["session_keys"]
    env["HERMES_SESSION_ID"] = "explicit-id"
    ctx.provider.synthesize("Hi.", "out.mp3")
    assert latest()["session_id"] == "explicit-id"


@pytest.mark.parametrize("raises", [False, True])
def test_validator_context_without_state(tmp_path, raises):
    class Probe(FakeContext):
        def __getattribute__(self, key):
            if raises and key == "state":
                raise RuntimeError("probe has no state")
            return super().__getattribute__(key)
    ctx = Probe()
    del ctx.state
    register_all(ctx, reader=lambda key, default=None: default, data_dir=lambda: tmp_path)
    assert ctx.command("status")


@pytest.mark.parametrize("piece,expected", [("[pause] Two.", .92),
    ("[[register: curious]] Two.", 1.03), ("[[register: unknown]] Two.", 1.0)])
def test_carry_after_a_cueless_piece_and_header(rig, piece, expected):
    ctx, fish, *_ = rig
    list(ctx.provider.stream("[sad] One."))
    list(ctx.provider.stream(piece))
    assert fish.calls[-1][2]["speed"] == expected
    list(ctx.provider.stream("Three."))
    assert fish.calls[-1][2]["speed"] == (1.03 if expected == 1.03 else .92)


def test_header_words_in_why_and_log(rig):
    ctx, _, _, _, _, directory = rig
    ctx.provider.synthesize("[[register: slightly sad; curious]] [calm] Hi.", "out.mp3")
    words = ["sad", "curious", "calm"]
    assert latest()["cue_words"] == words
    assert "authored cue words: sad, curious, calm" in ctx.command("why")
    assert json.loads((directory / "delivery.jsonl").read_text())["cue_words"] == words


@pytest.mark.parametrize("value,expected", [(False, "off"), (True, "off"), (None, "off"), ("no", "off"),
    ("unknown", "off"), (" FACTS ", "facts"), (" Facts+Affordance ", "facts+affordance")])
def test_normalized_signals(rig, value, expected):
    ctx, *_ = rig
    ctx.set_config("signals", value)
    ctx.hooks["post_tool_call"](session_id="fixture-session", status="error")
    line = ctx.hooks["pre_llm_call"](session_id="fixture-session")
    assert bool(line) == (expected != "off")
    assert f"signals: {expected}" in ctx.command("status")
    if line:
        assert ("Stopping and reporting is fine." in line["context"]) == (expected == "facts+affordance")


@pytest.mark.parametrize("quiet,platform,expected", [(True, "telegram", "[sad] Hi."),
    (False, "slack", "[sad] Hi. [sighing]"), (False, "email", "[sad] Hi. [sighing]")])
def test_middleware_quiet(rig, quiet, platform, expected):
    ctx, _, host, env, *_ = rig
    host["tts.provider"] = "fish-audio"
    env["HERMES_SESSION_PLATFORM"] = platform
    if quiet:
        ctx.command("quiet on")
    result = ctx.middleware(tool_name="text_to_speech", args={"text": "[sad] Hi. [sigh]"})
    assert result["args"]["text"] == expected


def test_counter_reset_and_truthful_success(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    hooks = ctx.hooks
    hooks["post_tool_call"](session_id="fixture-session", status="ok")
    assert hooks["pre_llm_call"](session_id="fixture-session") is None
    hooks["post_tool_call"](session_id="fixture-session", status="error")
    assert ctx.state["sessions"]["fixture-session"]["successes_since_error"] == 0
    hooks["post_tool_call"](session_id="fixture-session", status="ok")
    assert "1 tool success since error" in hooks["pre_llm_call"](session_id="fixture-session")["context"]
    hooks["post_tool_call"](session_id="fixture-session", status="error")
    assert ctx.state["sessions"]["fixture-session"]["successes_since_error"] == 0


@pytest.mark.parametrize("key,label", [("tool_error_streak", "tool error in a row"),
    ("transport_errors", "transport error"), ("retries", "API retry"), ("interruptions", "interruption")])
def test_singular_counter_labels(key, label):
    line = situation_line({key: 1}, 10000)
    assert f"1 {label}" in line and f"1 {label}s" not in line


@pytest.mark.parametrize("name", [" FISH-AUDIO ", " EMOTION-CHIP ", " ELEVENLABS "])
def test_tts_provider_normalization(rig, name):
    ctx, _, host, *_ = rig
    host["tts.provider"] = name
    host["tts.elevenlabs.model_id"] = "eleven_v4"
    assert ctx.provider.settings.tts_provider() == name.strip().lower()
    assert bool(ctx.section({})) == (name.strip().lower() == "emotion-chip")
    result = ctx.middleware(tool_name="text_to_speech", args={"text": "[soft: here]"})
    assert (result is None) == (name.strip().lower() == "emotion-chip")


def test_fish_paren_adapter_runs_before_render(rig, monkeypatch):
    ctx, fish, *_ = rig
    calls = []
    def adapter(text, family):
        calls.append((text, family))
        return text.replace("(sobbing)", "[sobbing]").replace("(sighing)", "[sighing]")
    # Resolve through the loaded provider's package; no Fish package is imported.
    from types import SimpleNamespace
    monkeypatch.setattr(importlib, "import_module", lambda name: SimpleNamespace(adapt_tags=adapter))
    text = "(sobbing) Hi. (sighing) Next. (sighing) Last."
    ctx.provider.synthesize(text, "out.mp3")
    assert calls[0] == (text, "S2") and {family for _, family in calls} == {"S2"}
    assert fish.calls[-1][1] == "[sobbing] Hi. [sighing] Next. [sighing] Last."
    assert latest()["dropped"] == []


def test_fish_adapter_injectable_and_missing(rig, monkeypatch):
    ctx, fish, *_ = rig
    calls = []
    ctx.provider.tag_adapter = lambda text, family: calls.append(family) or text
    ctx.provider.synthesize("Hi.", "out.mp3")
    assert calls and set(calls) == {"S2"}
    ctx.provider.tag_adapter = None
    def missing(name):
        raise ImportError("fixture missing tags")
    monkeypatch.setattr(importlib, "import_module", missing)
    ctx.provider.synthesize("Hi.", "out.mp3")
    assert any("adapt_tags unavailable" in n for n in latest()["notes"])


@pytest.mark.parametrize("mode,result", [("facts", '{"error":"fixture"}'), ("facts", "fixture error"),
                                        ("facts+affordance", '{"error":"fixture"}')])
def test_transform_tool_result_mid_task(rig, mode, result):
    ctx, *_ = rig
    ctx.set_config("signals", mode)
    hook = ctx.hooks["transform_tool_result"]
    kw = {"session_id": "fixture-session", "tool_name": "terminal"}
    assert hook(**kw, result=result) is None
    for _ in range(2):
        ctx.hooks["post_tool_call"](**kw, status="error")
    transformed = hook(**kw, result=result)
    if result.startswith("{"):
        value = json.loads(transformed)
        assert value["error"] == "fixture"
        line = value["situation"]
    else:
        assert transformed.startswith(result + "\n\nSituation: ")
        line = transformed.split("\n\nSituation: ", 1)[1]
    assert "2 tool errors in a row" in line
    assert ("Stopping and reporting is fine." in line) == (mode == "facts+affordance")
    assert ctx.state["sessions"]["fixture-session"]["last_situation"] == line
    assert hook(session_id="fixture-session", tool_name="text_to_speech", result=result) is None
    ctx.set_config("signals", False)
    assert hook(**kw, result=result) is None
    ctx.set_config("signals", "facts")
    ctx.hooks["post_tool_call"](**kw, status="ok")
    assert hook(**kw, result=result) is None


def test_transform_default_signals_never_rewrites(rig):
    ctx, *_ = rig
    for _ in range(3):
        ctx.hooks["post_tool_call"](status="error")
    assert ctx.hooks["transform_tool_result"](result='{"error":"x"}', status="error", tool_name="terminal") is None


def test_transform_uses_call_status_not_shared_streak(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    post, transform = ctx.hooks["post_tool_call"], ctx.hooks["transform_tool_result"]
    kw = {"session_id": "fixture-session", "tool_name": "terminal"}
    for status in ("error", "ok"):
        post(**kw, status=status)
    assert transform(**kw, status="error", result='{"error":"A"}') is not None
    assert transform(**kw, result="err") is None
    for status in ("ok", "error"):
        post(**kw, status=status)
    assert transform(**kw, status="ok", result='{"output":"B"}') is None
    assert transform(**kw, result="err") is not None


def test_transform_counts_this_failure_in_either_hook_order(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    post, transform = ctx.hooks["post_tool_call"], ctx.hooks["transform_tool_result"]
    kw = {"session_id": "fixture-session", "tool_name": "terminal"}
    # Hermes's serial path: transform_tool_result runs before this call's post_tool_call.
    first = transform(**kw, status="error", tool_call_id="c1", result="err")
    assert "1 tool error in a row" in first
    post(**kw, status="error", tool_call_id="c1")
    assert transform(**kw, status="ok", tool_call_id="c2", result="ok") is None
    post(**kw, status="ok", tool_call_id="c2")
    after_success = transform(**kw, status="error", tool_call_id="c3", result="err")
    assert "1 tool error in a row" in after_success and "success" not in after_success
    post(**kw, status="error", tool_call_id="c3")
    # The concurrent path: post_tool_call first; the failure is not counted twice.
    post(**kw, status="error", tool_call_id="c4")
    assert "2 tool errors in a row" in transform(**kw, status="error", tool_call_id="c4", result="err")
    assert ctx.state["sessions"]["fixture-session"]["tool_error_streak"] == 2


def test_transform_preserves_json_object_bytes(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    ctx.hooks["post_tool_call"](status="error")
    transform = ctx.hooks["transform_tool_result"]
    result = '{\n  "error": "x",\n  "n": 1.0e5\n}'
    transformed = transform(result=result, status="error", tool_name="terminal")
    line = ctx.state["sessions"][""]["last_situation"]
    assert transformed.startswith('{\n  "error": "x",\n  "n": 1.0e5')
    assert json.loads(transformed)["situation"] == line
    result = '{"error":"x","situation":"tool"}'
    assert transform(result=result, status="error") == result + "\n\nSituation: " + line
    assert json.loads(transform(result="{}", status="error")) == {"situation": line}


def test_transform_signals_off_reads_no_state(rig):
    ctx, *_ = rig
    class Counting(dict):
        reads = 0
        def get(self, *args):
            self.reads += 1
            return super().get(*args)
    hooks = ctx.hooks["transform_tool_result"].__self__
    state = Counting()
    hooks.situation.state, hooks.env = state, lambda key: ""
    assert hooks.transform_tool_result(session_id="", tool_name="terminal", result="x") is None
    assert state.reads == 0


def test_transform_empty_session_id_matches_post_tool_call(rig):
    ctx, _, _, env, *_ = rig
    env["HERMES_SESSION_ID"] = "S-env"
    ctx.set_config("signals", "facts")
    for _ in range(2):
        ctx.hooks["post_tool_call"](session_id="", status="error")
    result = ctx.hooks["transform_tool_result"](session_id="", tool_name="terminal", result="err", status="error")
    line = ctx.state["sessions"][""]["last_situation"]
    assert "2 tool errors in a row" in result and result == "err\n\nSituation: " + line


def test_transform_deeply_nested_json_falls_back_to_text(rig):
    ctx, *_ = rig
    ctx.set_config("signals", "facts")
    ctx.hooks["post_tool_call"](status="error")
    result = "[" * 200000 + "]" * 200000
    transformed = ctx.hooks["transform_tool_result"](result=result, status="error", tool_name="terminal")
    assert transformed == result + "\n\nSituation: " + ctx.state["sessions"][""]["last_situation"]


def test_stream_pieces_keep_every_sound(rig):
    ctx, fish, *_ = rig
    ctx.hooks["pre_llm_call"](session_id="fixture-session")
    for piece in ("[laugh] One.", "[laugh] Two.", "[sobbing] Three.", "[laugh] [laugh] Four."):
        list(ctx.provider.stream(piece))
    assert [row[1] for row in fish.calls] == ["[laughing] One.", "[laughing] Two.", "[sobbing] Three.",
                                              "[laughing] [laughing] Four."]


@pytest.fixture
def fish_tags(rig, monkeypatch):
    """Fish's adapter resolved from the loaded Fish provider's package; FakeFish adapts again before voicing."""
    ctx, fish, *_ = rig
    expected = type(fish).__module__.rsplit(".", 1)[0] + ".tags"
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *rest: SimpleNamespace(
        adapt_tags=fish_like_adapt_tags) if name == expected else real(name, *rest))
    fish.adapt = fish_like_adapt_tags
    return rig


@pytest.mark.parametrize("quiet,text,voiced", [
    (False, "Done. [hysterical] (sobbing) Sorry.", "Done. [hysterical] [sobbing] Sorry."),
    (False, "[[register: hysterical]] (sobbing) I'm sorry.", "[sobbing] I'm sorry."),
    (False, "[sigh] One. [hysterical] (sighing) Two.", "[sighing] One. [hysterical] [sighing] Two."),
    (True, "[sigh] One. [hysterical] (sighing) Two.", "One. [hysterical] Two."),
    (True, "Ok. [hysterical] (sighing) fine.", "Ok. [hysterical] fine."),
])
def test_parens_fish_adapts_are_kept_unless_quiet(fish_tags, quiet, text, voiced):
    ctx, fish, *_ = fish_tags
    if quiet:
        ctx.command("quiet on")
    ctx.provider.synthesize(text, "out.mp3")
    list(ctx.provider.stream(text))
    assert fish.spoken == [voiced, voiced] and [row[1] for row in fish.calls] == [voiced, voiced]


def test_parens_adapted_after_render_name_their_cue_words(fish_tags):
    ctx, *_ = fish_tags
    ctx.provider.synthesize("Done. [hysterical] (sobbing) Sorry.", "out.mp3")
    assert latest()["cue_words"] == ["hysterical", "sobbing"]


def test_compat_path_adapts_parens_and_follows_tts_model(fish_tags):
    ctx, fish, host, session, *_ = fish_tags
    host["tts.provider"] = "fish-audio"
    host.pop("tts.fish-audio.model")
    def call(text):
        result = ctx.middleware(tool_name="text_to_speech", args={"text": text})
        return result and result["args"]["text"]
    assert call("(sobbing) Sorry.") == "[sobbing] Sorry."
    assert call("[calm] Hi. (sobbing) Sorry.") == "[calm] Hi. [sobbing] Sorry."
    assert call("Done. [hysterical] (sobbing) Sorry.") == "Done. [hysterical] [sobbing] Sorry."
    assert call("Plain words.") is None
    assert call("Ok. (sighing) fine.") == "Ok. [sighing] fine."
    ctx.command("quiet on")
    assert call("Ok. (sighing) fine.") == "Ok. fine."
    ctx.command("quiet off")
    # The chip's own fish_model never reaches Fish on this path; Hermes passes tts.model.
    ctx.set_config("fish_model", "drama-3")
    assert call("[soft: here]") == "[soft tone] here"
    host["tts.model"] = "drama-3"
    assert call("[soft: here]") == "<soft>here</soft>"


def test_adapter_resolved_from_the_fish_package_on_fish_dialects_only(fish_tags, monkeypatch):
    ctx, fish, host, *_ = fish_tags
    names, resolve = [], importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *rest: names.append(name) or resolve(name, *rest))
    ctx.provider.synthesize("(sobbing) Hi.", "out.mp3")
    assert names == [type(fish).__module__.rsplit(".", 1)[0] + ".tags"] and fish.spoken[-1] == "[sobbing] Hi."
    # drama-3: Fish does not adapt parens there, so the chip translates them like bracket cues.
    host["tts.fish-audio.model"], fish.pinned_model, fish.adapt = "drama-3", "drama-3", None
    ctx.provider.synthesize("(sobbing) Hi.", "out.mp3")
    assert fish.calls[-1][1] == "[sobbing] Hi."
    ctx.provider.synthesize("Done. [hysterical] (sobbing) Sorry. (happy) Fine.", "out.mp3")
    assert fish.calls[-1][1] == "Done. [hysterical] [sobbing] Sorry. [happy] Fine."
    # s1 renders tagless, and S1 itself performs parens: the paren is translated, then dropped like any cue.
    host["tts.fish-audio.model"], fish.pinned_model = "s1", "s1"
    ctx.provider.synthesize("(sobbing) Hi. (happy) Fine.", "out.mp3")
    assert fish.calls[-1][1] == "Hi. Fine."


def test_subagent_turn_never_repoints_the_parent_key(rig):
    ctx, _, _, env, *_ = rig
    env.update(HERMES_SESSION_ID="", HERMES_SESSION_KEY="telegram:1")
    ctx.hooks["pre_llm_call"](session_id="parent-1", sender_id="fixture-sender")
    ctx.hooks["on_session_start"](session_id="child-9", platform="subagent")
    ctx.hooks["pre_llm_call"](session_id="child-9", parent_session_id="parent-1", platform="subagent")
    ctx.provider.synthesize("[[register: sad]] Done.", "out.mp3")
    assert latest()["session_id"] == "parent-1"
    assert ctx.command("register") == "[[register: sad]]" and "authored cue words: sad" in ctx.command("why")


def test_why_survives_another_sessions_render(rig):
    ctx, _, _, session, *_ = rig
    ctx.provider.synthesize("[sad] Ha.", "out.mp3")
    session["HERMES_SESSION_ID"] = "other-fixture-session"
    ctx.provider.synthesize("Plain.", "out.mp3")
    assert "authored cue words: none" in ctx.command("why")
    session["HERMES_SESSION_ID"] = "fixture-session"
    assert "authored cue words: sad" in ctx.command("why")


def test_long_reply_keeps_its_opening_cue(rig):
    ctx, fish, _, _, now, _ = rig
    list(ctx.provider.stream("[sad] One."))
    for index in range(6):
        now[0] += 30
        list(ctx.provider.stream(f"Piece {index}."))
    assert {row[2]["speed"] for row in fish.calls} == {0.92}


@pytest.mark.parametrize("bad,expected", [(float("nan"), 1.0), (float("inf"), 1.0), (float("-inf"), 1.0),
                                          ("fast", 1.0), (True, 1.0), (10**400, 2.0), (-10**400, 0.5)])
def test_speed_follows_fish_number_rules(rig, bad, expected):
    ctx, fish, host, *_ = rig
    ctx.provider.synthesize("Hi.", "out.mp3", speed=bad)
    assert fish.calls[-1][2]["speed"] == expected
    host["tts.fish-audio.speed"] = bad
    ctx.provider.synthesize("Hi.", "out.mp3")
    assert fish.calls[-1][2]["speed"] == expected


def test_status_prompt_and_skill_follow_tts_model(rig, tmp_path):
    ctx, _, host, *_ = rig
    host.pop("tts.fish-audio.model")
    host["tts.model"] = "drama-3"
    assert "dialect: fish_drama3" in ctx.command("status")
    assert ctx.section({"model": "default"}) == build_prompt_section("fish_drama3", "default", load_data())


@pytest.mark.parametrize("model", ["s2.1-pro", "drama-3"])
@pytest.mark.parametrize("text,voiced", [
    ("[sad] (sobbing) Sorry.", "[sad] [sobbing] Sorry."),
    ("[[register: sad]] [hysterical] (sobbing) Sorry.", "[hysterical] [sobbing] Sorry."),
    ("Done. [hysterical] " + "(sobbing) " * 9 + "Sorry.", "Done. [hysterical] " + "[sobbing] " * 9 + "Sorry."),
    ("I was (sobbing) yesterday.", "I was (sobbing) yesterday."),
    # Fish's opaque regions stay as written: the paren is not converted there.
    ("`Example. [sad] (sobbing) text`", "`Example. [sad] (sobbing) text`"),
    ("Hi. `code [sad] (sobbing) here", "Hi. `code [sad] (sobbing) here"),
])
def test_every_leading_paren_cue_is_rendered(fish_tags, model, text, voiced):
    ctx, fish, host, *_ = fish_tags
    host["tts.fish-audio.model"] = fish.pinned_model = model
    ctx.provider.synthesize(text, "out.mp3")
    assert fish.calls[-1][1] == voiced and fish.spoken[-1] == voiced


@pytest.mark.parametrize("model,voiced", [("s2.1-pro", "[soft tone] I was (sobbing) yesterday."),
                                         ("drama-3", "<soft>I was</soft> (sobbing) yesterday.")])
def test_a_paren_after_a_span_is_prose(fish_tags, model, voiced):
    ctx, fish, host, *_ = fish_tags
    host["tts.fish-audio.model"] = fish.pinned_model = model
    ctx.provider.synthesize("[soft: I was] (sobbing) yesterday.", "out.mp3")
    assert fish.calls[-1][1] == voiced
    host["tts.provider"] = "fish-audio"
    host["tts.model"] = model
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[soft: I was] (sobbing) yesterday."}
                          )["args"]["text"] == voiced


def test_adapt_all_settles_chains_outside_opaque_regions():
    assert adapt_all("Done. [sad] (happy) (sobbing) Bye.", fish_like_adapt_tags) == "Done. [sad] [happy] [sobbing] Bye."
    assert adapt_all("<|. [sad] (sobbing) x|> Ok.", fish_like_adapt_tags) == "<|. [sad] (sobbing) x|> Ok."
    # The sentence boundary is read in the whole text, so an opaque region does not start a sentence.
    assert adapt_all("See `x` [sad] (happy) now.", fish_like_adapt_tags) == "See `x` [sad] (happy) now."
    assert adapt_all("Ok. [Soft: a] (happy) b.", fish_like_adapt_tags) == "Ok. [Soft: a] (happy) b."


def test_compat_path_adapts_parens_on_drama3(fish_tags):
    ctx, fish, host, *_ = fish_tags
    host["tts.provider"] = "fish-audio"
    host["tts.model"] = host["tts.fish-audio.model"] = "drama-3"
    result = ctx.middleware(tool_name="text_to_speech", args={"text": "Done. [hysterical] (sobbing) Sorry. (happy) Ok."})
    assert result["args"]["text"] == "Done. [hysterical] [sobbing] Sorry. [happy] Ok."


def test_settling_keeps_the_register_params(fish_tags):
    ctx, fish, *_ = fish_tags
    # The stray "]" hides the paren until the parser drops it, so only a second render pass catches it.
    for text, voiced in (("[[register: sad]] [excited] Hi. ] (sobbing) Bye.", "[excited] Hi. [sobbing] Bye."),
                         ("[[register: sad]] [excited] Hi. ] " + "(sobbing) " * 9 + "Bye.",
                          "[excited] Hi. " + "[sobbing] " * 9 + "Bye.")):
        ctx.provider.synthesize(text, "out.mp3")
        assert fish.spoken[-1] == voiced and fish.calls[-1][2]["speed"] == 0.92


def test_settling_applies_the_quiet_switch(fish_tags):
    ctx, fish, *_ = fish_tags
    ctx.provider.synthesize("[excited] Hi. ] (sighing) Bye.", "out.mp3")
    assert fish.spoken[-1] == "[excited] Hi. [sighing] Bye."
    ctx.command("quiet on")
    ctx.provider.synthesize("[excited] Hi. ] (sighing) Bye.", "out.mp3")
    assert fish.spoken[-1] == "[excited] Hi. Bye."


def test_status_ready(rig, monkeypatch):
    ctx, fish, host, _, _, _ = rig
    status = ctx.command("status")
    assert status.startswith("ready: yes; backend: fish")
    fish.available = False
    assert "ready: no (the fish-audio plugin is not ready; check its FISH_API_KEY)" in ctx.command("status")
    monkeypatch.setattr(provider_module, "_fish", lambda: None)
    host["tts.provider"] = "edge"
    status = ctx.command("status")
    assert status.startswith("ready: no (the speech provider is edge; set tts.provider: emotion-chip); backend")
    host["tts.provider"] = "emotion-chip"
    ctx.settings.update(backend="eleven", eleven_voice_id="")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    status = ctx.command("status")
    assert "add ELEVENLABS_API_KEY to the profile's .env" in status and "set the plugin's eleven_voice_id" in status
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    ctx.settings.update(eleven_voice_id="voice-1")
    assert ctx.command("status").startswith("ready: yes; backend: eleven")


def test_status_key_unknown_in_unscoped_multiplex(rig, monkeypatch):
    import types
    from emotion_chip.hermes import commands
    ctx, fish, host, _, _, _ = rig

    class Unscoped(RuntimeError):
        pass

    def get_secret(name):
        raise Unscoped()
    monkeypatch.setitem(sys.modules, "agent.secret_scope",
                        types.SimpleNamespace(UnscopedSecretError=Unscoped, get_secret=get_secret))
    assert commands._has_secret("ELEVENLABS_API_KEY") is None
    fish.available = False  # Fish would read an empty key here too; status must not blame it, nor call it ready
    assert ctx.command("status").startswith("ready: probably (not checked from here: the Fish Audio key); backend")
    ctx.settings.update(backend="eleven", eleven_voice_id="voice-1")
    assert ctx.command("status").startswith("ready: probably (not checked from here: the ElevenLabs key); backend")
    ctx.settings.update(eleven_voice_id="")
    assert ctx.command("status").startswith("ready: no (set the plugin's eleven_voice_id); backend")


def test_status_other_provider_and_command_provider(rig, monkeypatch):
    ctx, fish, host, _, _, _ = rig
    host["tts.provider"] = "elevenlabs"
    monkeypatch.setattr(provider_module, "_fish", lambda: None)
    assert ctx.command("status").startswith("ready: no (the speech provider is elevenlabs; set tts.provider: "
                                            "emotion-chip); backend")
    host["tts.provider"] = "emotion-chip"
    host["tts.providers"] = {"emotion-chip": {"type": "command", "command": "say"}}
    assert "a command provider named emotion-chip" in ctx.command("status")


def test_status_survives_a_broken_fish(rig):
    ctx, fish, _, _, _, _ = rig
    def boom():
        raise RuntimeError("provider bug")
    fish.is_available = boom
    assert ctx.command("status").startswith(
        "ready: probably (not checked from here: the fish-audio plugin, whose readiness check raised an error); backend")


def test_status_reports_voice_reply_coverage(rig):
    from types import SimpleNamespace
    ctx, _, host, _, _, _ = rig

    def coverage():
        return ctx.command("status").rsplit("voice_replies: ", 1)[1]

    def message(platform="telegram"):
        source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id="42", message_id="7")
        return SimpleNamespace(source=source, message_id="7")

    class Gateway:
        def __init__(self, answer):
            self.answer = answer

        def _should_send_voice_reply(self, event, response, agent_messages, already_sent=False):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    assert coverage() == "not checked yet (the chip has seen no message from a covered platform in this process)"
    ctx.hooks["pre_gateway_dispatch"](event=message("discord"), gateway=SimpleNamespace())
    assert coverage().startswith("not checked yet")  # Discord is never asked, so it proves nothing either way
    ctx.hooks["pre_gateway_dispatch"](event=message(), gateway=Gateway(False))
    assert coverage() == "covered, except on Discord"  # Hermes answered: this chat has voice replies off
    ctx.hooks["pre_gateway_dispatch"](event=message(), gateway=SimpleNamespace())
    assert coverage() == "not covered (this Hermes has no voice-reply check)"
    ctx.hooks["pre_gateway_dispatch"](event=message(), gateway=Gateway(RuntimeError("bug")))
    assert coverage() == "not covered (Hermes's voice-reply check failed)"
    host["tts.provider"] = "elevenlabs"
    assert coverage() == "off (needs tts.provider: emotion-chip and guide_mode: generate)"


def test_status_voice_replies_need_a_restart_after_a_late_switch(tmp_path, monkeypatch):
    ctx, fish = FakeContext(), FakeFish()
    host = {"tts.provider": "edge"}
    monkeypatch.setattr(provider_module, "_fish", lambda: fish)
    register_all(ctx, reader=lambda key, default=None: host.get(key, default), data_dir=lambda: tmp_path / "plugin-data",
                 env=lambda key: "", clock=lambda: 10000.0)
    assert "pre_gateway_dispatch" not in ctx.hooks
    host["tts.provider"] = "emotion-chip"
    assert ctx.command("status").endswith(
        "voice_replies: off until Hermes restarts (the chip read other settings when it started)")
