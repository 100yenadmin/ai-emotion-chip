"""Voice replies fact: Hermes's saved /voice setting for the chat, read by the gateway's own rule."""
import json
import os
from pathlib import Path

import pytest

from emotion_chip.hermes.hooks import Hooks
from emotion_chip.hermes.settings import Settings
from emotion_chip.hermes.situation import Situation
from emotion_chip.hermes.voice import FACTS, voice_fact, voice_replies
from fakes import FakeContext


def settings(host=None, **plugin):
    host = {"tts.provider": "emotion-chip", **(host or {})}
    return Settings(FakeContext(plugin), lambda key, default=None: host.get(key, default))


def env(platform="telegram", chat="42", profile="chipsmoke"):
    values = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat, "HERMES_SESSION_PROFILE": profile}
    return lambda key, default="": values.get(key, default)


def home(tmp_path, modes):
    (tmp_path / "gateway_voice_mode.json").write_text(modes if isinstance(modes, str) else json.dumps(modes))
    return tmp_path


@pytest.mark.parametrize("modes, auto_tts, expected", [
    ({"chipsmoke:telegram:42": "all"}, False, "all"),
    ({"telegram:42": "voice_only"}, False, "voice"),
    ({"chipsmoke:telegram:42": "off"}, True, None),          # an explicit /voice off wins over voice.auto_tts
    ({}, True, "voice"),                                      # no chat mode: voice.auto_tts voices voice messages
    ({}, False, None),
    ({"other:telegram:42": "all"}, False, "all"),             # a lone match under the bot owner's profile name
    ({"a:telegram:42": "all", "b:telegram:42": "off"}, True, "voice"),  # ambiguous: fall back to voice.auto_tts
    ({"chipsmoke:telegram:420": "all", "telegram:4": "all"}, False, None),  # other chats never match
    ("not json", True, "voice"),
    ('["all"]', False, None),
])
def test_voice_replies_rule(tmp_path, modes, auto_tts, expected):
    assert voice_replies(settings({"voice.auto_tts": auto_tts}), env(), home(tmp_path, modes)) == expected


@pytest.mark.parametrize("platform, chat", [("cli", "42"), ("local", "42"), ("desktop", "42"), ("", "42"),
                                            ("telegram", "")])
def test_no_voice_notes_there(tmp_path, platform, chat):
    path = home(tmp_path, {f"{platform}:{chat}": "all", f"chipsmoke:{platform}:{chat}": "all"})
    assert voice_replies(settings({"voice.auto_tts": True}), env(platform, chat), path) is None


def test_missing_file_uses_auto_tts(tmp_path):
    assert voice_replies(settings({"voice.auto_tts": True}), env(), tmp_path) == "voice"
    assert voice_replies(settings(), env(), tmp_path) is None


def test_fact_only_with_the_chip_guide(tmp_path):
    path = home(tmp_path, {"chipsmoke:telegram:42": "all"})
    assert voice_fact(settings(), env(), path) == FACTS["all"]
    assert voice_fact(settings(guide_mode="defer"), env(), path) == ""      # defer stays a passthrough
    assert voice_fact(settings({"tts.provider": "elevenlabs"}), env(), path) == ""
    path = home(tmp_path, {"chipsmoke:telegram:42": "voice_only"})
    assert "quoted transcript" in voice_fact(settings(), env(), path)


def test_pre_llm_call_adds_the_fact(tmp_path):
    path = home(tmp_path, {"chipsmoke:telegram:42": "all"})
    hooks = Hooks(settings(), Situation({}, lambda: 1000.0), env(), voice_home=path)
    assert hooks.pre_llm_call(session_id="s1", platform="telegram") == {"context": FACTS["all"]}
    assert hooks.pre_llm_call(session_id="s2", platform="telegram", parent_session_id="s1") is None
    hooks = Hooks(settings(signals="facts"), Situation({}, lambda: 1000.0), env(), voice_home=path)
    hooks.situation.update("s3", lambda row: row.update(tool_error_streak=3))
    context = hooks.pre_llm_call(session_id="s3", platform="telegram")["context"]
    assert context.endswith("\n" + FACTS["all"])


def test_pre_llm_call_survives_a_broken_reader(tmp_path, monkeypatch):
    import emotion_chip.hermes.hooks as hooks_module
    monkeypatch.setattr(hooks_module, "voice_fact", lambda *a: 1 / 0)
    hooks = Hooks(settings(), Situation({}, lambda: 1000.0), env(), voice_home=tmp_path)
    assert hooks.pre_llm_call(session_id="s1", platform="telegram") is None


def test_hermes_voice_contract():
    root = Path(os.environ.get("HERMES_AGENT_SRC") or Path.home() / ".hermes" / "hermes-agent")
    if not root.is_dir():
        pytest.skip("Hermes source absent")
    run = (root / "gateway" / "run.py").read_text()
    assert '_VOICE_MODE_PATH = _hermes_home / "gateway_voice_mode.json"' in run
    voice = (root / "gateway" / "run_voice.py").read_text()
    assert '_VOICE_MODES = {"off", "voice_only", "all"}' in voice
    assert 'base = f"{platform.value}:{chat_id}"' in voice
    assert 'return base if not profile or profile == "default" else f"{profile}:{base}"' in voice
    assert '"auto_tts": False' in (root / "hermes_cli" / "config_defaults.py").read_text()
