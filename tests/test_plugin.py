"""M1 fake operating paths and text-only Hermes API contracts."""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

from emotion_chip.data import DIALECTS
from emotion_chip.guide import build_prompt_section
from emotion_chip.hermes import provider as provider_module
from emotion_chip.hermes import log as log_module
from emotion_chip.hermes.hooks import HOOKS
from emotion_chip.hermes.registration import model_key, register_all
from emotion_chip.hermes.settings import DEFAULTS, Settings
from emotion_chip.hermes.situation import situation_line
from fakes import FakeContext, FakeFish


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
               ("backend", "fish_model", "dialect", "guide_mode", "signals", "sounds_per_turn", "quiet", "delivery_log"))


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


def test_root_entry(rig, monkeypatch):
    import emotion_chip.hermes.registration as registration
    seen = []
    monkeypatch.setattr(registration, "register_all", seen.append)
    spec = importlib.util.spec_from_file_location("plugin_entry", Path(__file__).parents[1] / "__init__.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.register(rig[0])
    assert seen == [rig[0]]


def test_provider_fish_s2_and_drama(rig, runtime_data):
    ctx, fish, *_ = rig
    metadata = {}
    result = ctx.provider.synthesize("[slightly frustrated] Again. [sigh] [pause] Checking.",
                                     "out.mp3", voice="fixture", speed=1.2, result_metadata=metadata)
    kind, text, args = fish.calls[-1]
    assert result == "out.mp3" and kind == "synthesize"
    assert text == "[slightly frustrated] Again. [sighing] [break] Checking."
    factor = runtime_data.groups[runtime_data.lookup("frustrated")["group"]]["fish_speed"]
    assert args == {"voice": "fixture", "model": "s2.1-pro", "speed": 1.2 * factor,
                    "format": "mp3", "result_metadata": metadata}
    assert args["result_metadata"] is metadata
    ctx.set_config("fish_model", "drama-3")
    ctx.provider.synthesize("[soft: here] [speaker: 2]", "out.wav", format="wav")
    assert fish.calls[-1][1] == "<soft>here</soft>" and fish.calls[-1][2]["model"] == "drama-3"
    ctx.provider.synthesize("[soft: here]", "out.mp3", model="s2.1-pro-free")
    assert fish.calls[-1][1] == "[soft tone] here" and fish.calls[-1][2]["model"] == "s2.1-pro-free"
    ctx.provider.synthesize("[soft: here]", "out.mp3", model="not-a-fish-model")
    assert fish.calls[-1][2]["model"] == "drama-3"


@pytest.mark.parametrize("base, expected", [(0, 0.92), (0.1, 0.5), (3, 2.0)])
def test_speed_bounds(rig, base, expected):
    ctx, fish, *_ = rig
    ctx.provider.synthesize("[sad] Again.", "out.mp3", speed=base)
    assert fish.calls[-1][2]["speed"] == expected


def test_business_and_quiet(rig):
    ctx, fish, _, session, *_ = rig
    session["HERMES_SESSION_PLATFORM"] = "slack"
    ctx.provider.synthesize("[sad] Hello. [sigh]", "out.mp3")
    assert fish.calls[-1][1] == "[sad] Hello."
    session["HERMES_SESSION_PLATFORM"] = "telegram"
    assert ctx.command("quiet on") == "Sounds quiet: on" and ctx.state["quiet"] is True
    list(ctx.provider.stream("[sigh] Hello."))
    assert fish.calls[-1][1] == "Hello."
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


def test_stream_concurrent_carry(rig):
    ctx, fish, *_ = rig
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
    ("fish-audio", "drama-3", "[soft tone] here [sighing]"),
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
    assert args["text"] == "[soft: here] [sigh]" and "<" not in expected


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
    assert set(row) == {"ts", "session_id", "sender_id", "platform", "dialect", "cue_words", "group", "params", "dropped_kinds", "chars"}
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


def test_commands_why_and_register(rig):
    ctx, *_ = rig
    assert ctx.command("register") == "No authored register recorded."
    ctx.provider.synthesize("[[register: steady]] [curious] One. [sigh] [laugh] [speaker: 2]", "out.mp3")
    why = ctx.command("why")
    assert "fish_s2" in why and "curious" in why and "params:" in why
    assert "laugh: sounds_per_turn" in why and "speaker 2: unsupported" in why
    assert "caps applied: sounds_per_turn" in why
    for unauthed in ("sad", "frustrated", "angry", "happy", "anxious", "bored"):
        assert unauthed not in why
    assert ctx.command("register") == "[[register: steady]]"
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
    root = Path.home() / ".hermes" / "hermes-agent"
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
    assert set(manifest) <= known and "requires_plugins" not in manifest
    assert set(manifest["provides_hooks"]) == set(HOOKS)
    assert manifest["manifest_version"] == 2 and manifest["requires_hermes"] == ">=0.22.2"
    assert {key: row["default"] for key, row in manifest["config_schema"].items()} == DEFAULTS
    assert not any(name in sys.modules for name in ("hermes_cli.plugins", "agent.tts_provider"))
