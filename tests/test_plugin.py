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
    assert ctx.calls == [("provider", "emotion-chip"), ("section", "emotion-chip"),
                         ("skill", "emotion-chip-voice"), *[("hook", name) for name in HOOKS],
                         ("middleware", "tool_request"), ("command", "chip")]
    path, description = ctx.skill
    assert path == directory / "skills" / "emotion-chip-voice" / "SKILL.md"
    assert description == path.read_text().split("description: ", 1)[1].splitlines()[0]
    assert ctx.max_chars == 4000
    assert ctx.command_info == ("Hermes Emotion Chip voice delivery", "[status|why|quiet|register]")
    assert all(f"{key}:" in ctx.command("status") for key in
               ("backend", "fish_model", "dialect", "guide_mode", "signals", "quiet", "delivery_log"))


def test_defer(tmp_path):
    ctx = FakeContext({"guide_mode": "defer"})
    directory = tmp_path / "unused"
    register_all(ctx, reader=lambda key, default=None: "emotion-chip", data_dir=lambda: directory)
    assert ctx.section({"model": "default"}) == ""
    assert not any(kind == "skill" for kind, _ in ctx.calls) and not directory.exists()
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[sad] Hello"}) is None


def test_guarded_registrations():
    class Minimal:
        state = {}

        def get_config(self, key, default=None):
            return default
    register_all(Minimal(), reader=lambda key, default=None: default)


def test_root_entry_loads_like_hermes(tmp_path):
    """Load the plugin as Hermes does (hermes_plugins.<slug>, plugin dir not on sys.path) in a clean interpreter."""
    root = Path(__file__).parents[1]
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
    assert ctx.command("quiet on") == "Sounds quiet: on" and ctx.state["quiet"] is True
    list(ctx.provider.stream("[sigh] [crying] Hello."))
    assert fish.calls[-1][1] == "[crying] Hello."
    ctx.command("quiet off")
    ctx.provider.synthesize("[sigh] Hello.", "out.mp3")
    assert fish.calls[-1][1] == "[sighing] Hello."
    assert ctx.command("quiet") == "Sounds quiet: on"


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
        ("register_hook", ["hook_name", "callback"], []), ("get_config", ["key", "default"], []),
    ):
        args = methods[name].args
        assert [a.arg for a in args.args[1:1+len(positional)]] == positional
        assert set(keywords) <= {a.arg for a in [*args.args, *args.kwonlyargs]}
    rows = _literal_assignment(plugins, "_SCOPED_PROVIDER_REGISTRARS")
    tts = next(row for row in rows if row[0] == "register_tts_provider")
    assert tts[2:4] == ("agent.tts_registry", "agent.tts_provider:TTSProvider")
    factory = next(n for n in plugins.body if isinstance(n, ast.FunctionDef) and n.name == "_make_scoped_provider_registrar")
    registrar = next(n for n in factory.body if isinstance(n, ast.FunctionDef) and n.name == "register")
    assert [a.arg for a in registrar.args.args] == ["self", "provider"]
    content = ast.parse((root / "hermes_cli" / "plugins_content.py").read_text())
    skill = next(n for n in content.body if isinstance(n, ast.FunctionDef) and n.name == "register_skill")
    assert [a.arg for a in skill.args.args][:4] == ["self", "name", "path", "description"]
    manifest_source = ast.parse((root / "hermes_cli" / "plugins_manifest.py").read_text())
    known = _literal_assignment(manifest_source, "_KNOWN_MANIFEST_FIELDS")
    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())
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
    register_all(ctx, reader=lambda name, default=None: "drama-3" if name == key else default,
                 data_dir=lambda: tmp_path)
    assert build_skill("fish_drama3", "default", load_data()) != build_skill("fish_s2", "default", load_data())
    assert ctx.skill[0].read_text() == build_skill("fish_drama3", "default", load_data()) + "\n"


@pytest.mark.parametrize("configured,call,expected", [
    (None, None, None), ("invalid", "invalid", None), ("drama-3", None, "drama-3"),
    ("drama-3", "s2-pro", "s2-pro"), ("drama-3-preview", None, "drama-3"),
])
def test_requested_model_and_wallet_default(rig, configured, call, expected):
    ctx, fish, host, *_ = rig
    host["tts.fish-audio.model"] = "invalid"
    if configured is not None:
        ctx.set_config("fish_model", configured)
    ctx.provider.synthesize("Hello.", "out.mp3", model=call)
    assert fish.calls[-1][2]["model"] == expected
    assert latest()["fish_model"] == (expected or "s2.1-pro")


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
