"""Offline agent tools and the bounded speech-result report."""
from copy import deepcopy
import argparse
import json
from types import SimpleNamespace
import sys

import pytest

from emotion_chip import cli
from emotion_chip.hermes import provider as provider_module
from emotion_chip.hermes.registration import register_all
from fakes import FakeContext, FakeFish, fish_like_adapt_tags


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ctx, fish = FakeContext(), FakeFish()
    host = {"tts.provider": "emotion-chip", "tts.fish-audio.model": "s2.1-pro"}
    session = {"HERMES_SESSION_ID": "tools-session"}
    now = [10000.0]
    monkeypatch.setattr(provider_module, "_fish", lambda: fish)
    with provider_module.last_render_lock:
        provider_module.last_render.clear()
    register_all(ctx, reader=lambda key, default=None: host.get(key, default),
                 data_dir=lambda: tmp_path / "plugin-data", env=lambda key: session.get(key, ""), clock=lambda: now[0])
    ctx.provider.tag_adapter = lambda text, family: text
    return ctx, host, session, now, tmp_path / "plugin-data"


def call(ctx, name, **args):
    return json.loads(ctx.tools[name]["handler"](args))


def speech(ctx, result='{"success": true}', **kwargs):
    return ctx.hooks["transform_tool_result"](
        tool_name="text_to_speech", result=result, **{"status": "ok", "session_id": "tools-session", **kwargs})


def prepare(ctx, score="[warm] Hello. [speaker: 2]", dialect="eleven"):
    ctx.settings["backend"] = dialect
    ctx.provider._prepare(score, None, None)


def test_registration_and_late_defer(rig):
    ctx = rig[0]
    assert set(ctx.tools) == {"chip_status", "chip_preview"}
    for name, tool in ctx.tools.items():
        assert tool["toolset"] == "emotion-chip"
        assert tool["schema"]["name"] == name
        assert tool["schema"]["description"] == tool["description"]
        assert tool["check_fn"]()
        assert not tool["is_async"] and tool["requires_env"] is None
    schema = ctx.tools["chip_preview"]["schema"]["parameters"]
    assert schema["required"] == ["score"]
    assert schema["properties"]["score"] == {"type": "string", "maxLength": 10000}
    assert schema["properties"]["dialect"]["enum"] == list(cli.DIALECTS)
    ctx.settings["guide_mode"] = "defer"
    assert all(not tool["check_fn"]() for tool in ctx.tools.values())


def test_defer_registers_no_tools(tmp_path):
    ctx = FakeContext({"guide_mode": "defer"})
    register_all(ctx, reader=lambda key, default=None: default, data_dir=lambda: tmp_path)
    assert ctx.tools == {}
    assert ctx.provider and ctx.hooks and ctx.command and ctx.cli_command and ctx.middleware


def test_host_without_register_tool(tmp_path):
    base = FakeContext()

    class OldHost:
        def __getattr__(self, name):
            if name == "register_tool":
                raise AttributeError(name)
            return getattr(base, name)

    register_all(OldHost(), reader=lambda key, default=None: default, data_dir=lambda: tmp_path)
    assert base.tools == {}
    assert base.provider and base.section and base.skill and base.hooks and base.command and base.cli_command


@pytest.mark.parametrize("case", ["yes", "no", "probably", "eleven"])
def test_status_matches_command(rig, monkeypatch, case):
    ctx, host, _, _, _ = rig
    if case == "no":
        host["tts.provider"] = "edge"
    elif case == "probably":
        class UnscopedSecretError(RuntimeError):
            pass

        def get_secret(name):
            raise UnscopedSecretError()

        monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "agent.secret_scope", SimpleNamespace(
            UnscopedSecretError=UnscopedSecretError, get_secret=get_secret))
    elif case == "eleven":
        ctx.settings.update(backend="eleven", eleven_voice_id="fixture-voice")
        host["tts.elevenlabs.model_id"] = "eleven_v4_turbo"
        monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-key")
    data = call(ctx, "chip_status")
    assert data["ready"] == ("yes" if case == "eleven" else case)
    assert set(data) == {"ready", "problems", "not_checked", "backend", "model", "dialect", "guide_mode",
                         "signals", "quiet", "delivery_log", "voice_replies", "session_id", "register",
                         "eleven_voice_id" if case == "eleven" else "fish_model"}
    assert data["session_id"] == "tools-session" and data["register"] == ""
    assert data["quiet"] is False and data["delivery_log"] is True
    ready = data["ready"]
    if data["problems"]:
        ready += " (" + "; ".join(data["problems"]) + ")"
    elif data["not_checked"]:
        ready += " (not checked from here: " + ", ".join(data["not_checked"]) + ")"
    fields = dict(ready=ready, backend=data["backend"])
    if case != "eleven":
        fields["fish_model"] = data["fish_model"]
        assert data["model"] == "s2.1-pro"
    fields.update(guide_mode=data["guide_mode"], signals=data["signals"])
    if case == "eleven":
        fields.update(eleven_model=data["model"], eleven_voice_id=data["eleven_voice_id"])
        assert data["model"] == "eleven_v4_turbo" and data["dialect"] == "eleven_v4"
    fields.update(session_id=data["session_id"], dialect=data["dialect"], quiet="off", delivery_log="on",
                  voice_replies=data["voice_replies"])
    assert ctx.command("status") == "; ".join(f"{key}: {value}" for key, value in fields.items())


def test_status_register_and_live_voice_coverage(rig):
    ctx, host, session, _, _ = rig
    prepare(ctx, "[[register: steady]] [warm] Hello.", "fish")
    ctx.command("quiet on")
    ctx.settings["delivery_log"] = False
    host["tts.fish-audio.model"] = "drama-3-preview"
    replies = ctx.hooks["pre_gateway_dispatch"].__self__.voice_replies
    replies.check = "ok"
    data = call(ctx, "chip_status")
    assert data["register"] == "[[register: steady]]"
    assert data["quiet"] is True and data["delivery_log"] is False
    assert data["fish_model"] == "drama-3" and data["model"] == "drama-3" and data["dialect"] == "fish_drama3"
    assert data["voice_replies"] == "covered, except on Discord"
    assert ctx.command("status").endswith("voice_replies: " + data["voice_replies"])
    session["HERMES_SESSION_ID"] = "another-session"
    assert call(ctx, "chip_status")["register"] == ""


@pytest.mark.parametrize("dialect", cli.DIALECTS)
@pytest.mark.parametrize("score", ["[warm] Hello.", "[whisper: Be quiet.]", "[sigh] It passed.",
                                   "[[register: steady]] [warm, amused] It passed."])
def test_preview_matches_cli(rig, capsys, dialect, score):
    ctx = rig[0]
    args = cli.add_commands(argparse.ArgumentParser()).parse_args(
        ["render", "--json", "--dialect", dialect, score])
    assert cli.run(args) == 0
    expected = json.loads(capsys.readouterr().out)
    result = call(ctx, "chip_preview", score=score, dialect=dialect)
    assert result["dialect"] == dialect
    assert result["rendered"] == expected["text"]
    assert result["params"] == expected["params"] and result["notes"] == expected["notes"]
    assert result["dropped"] == [dict(kind=d["kind"], raw=d["value"], reason=d["reason"]) for d in expected["dropped"]]
    assert result["chars"] == len(expected["text"])
    expected_words = ["steady", "warm", "amused"] if score.startswith("[[") else ["warm"] if score.startswith("[warm]") else []
    assert result["cue_words"] == expected_words


def test_preview_quiet_and_no_side_effects(rig):
    ctx, _, _, _, directory = rig
    ctx.command("quiet on")
    score = "[[register: warm]] [sigh] Hello."
    state = deepcopy(ctx.state)
    before = deepcopy(provider_module.last_render)
    result = call(ctx, "chip_preview", score=score)
    assert result["dialect"] == call(ctx, "chip_status")["dialect"]
    assert any(d["kind"] == "event" and d["raw"] == "sigh" for d in result["dropped"])
    assert "sigh" not in result["rendered"]
    assert not (directory / "delivery.jsonl").exists()
    assert ctx.state == state and provider_module.last_render == before
    rendered, _, _ = ctx.provider._prepare(score, None, None)
    assert result["rendered"] == rendered.text
    assert result["dropped"] == [dict(kind=d.kind, raw=d.value, reason=d.reason) for d in rendered.dropped]
    # Preview also leaves an existing explanation/log byte-identical.
    prepare(ctx)
    state, before = deepcopy(ctx.state), deepcopy(provider_module.last_render)
    logged = (directory / "delivery.jsonl").read_bytes()
    call(ctx, "chip_preview", score=score, dialect="prose")
    assert provider_module.last_render == before and ctx.state == state
    assert (directory / "delivery.jsonl").read_bytes() == logged


def preview_without_side_effects(rig, **args):
    ctx, _, _, _, directory = rig
    state = deepcopy(ctx.state)
    before = deepcopy(provider_module.last_render)
    carried = deepcopy(ctx.provider._carried)
    result = call(ctx, "chip_preview", **args)
    assert not (directory / "delivery.jsonl").exists()
    assert ctx.state == state and provider_module.last_render == before
    assert ctx.provider._carried == carried
    assert set(result) == {"dialect", "rendered", "cue_words", "dropped", "params", "notes", "chars"}
    return result


def assert_preview_matches_render(result, rendered):
    assert result["rendered"] == rendered.text
    assert result["dropped"] == [dict(kind=d.kind, raw=d.value, reason=d.reason) for d in rendered.dropped]
    assert result["params"] == rendered.params
    assert result["notes"] == rendered.notes
    assert result["chars"] == len(rendered.text)


@pytest.mark.parametrize("dialect", [{}, {"dialect": None}, {"dialect": "fish_s2"}])
@pytest.mark.parametrize("quiet", [False, True])
def test_preview_fish_parens_match_provider(rig, dialect, quiet):
    ctx = rig[0]
    calls = []

    def adapter(text, family):
        calls.append(family)
        return text.replace("(laughs)", "[laughs]")

    ctx.provider.tag_adapter = adapter
    if quiet:
        ctx.command("quiet on")
    score = "(laughs) It passed."
    result = preview_without_side_effects(rig, score=score, **dialect)
    assert calls and set(calls) == {"S2"}
    # A laugh is a sound event, not a cue word; the adapter shows in the render or, quiet, in the drops.
    assert result["cue_words"] == []
    if quiet:
        assert any(d["kind"] == "event" and d["raw"] == "laugh" for d in result["dropped"])
        assert result["rendered"] == "It passed."
    else:
        assert result["rendered"] == "[laughing] It passed."
    rendered, _, _ = ctx.provider._prepare(score, None, None)
    assert_preview_matches_render(result, rendered)
    assert result["cue_words"] == provider_module.last_render["tools-session"]["cue_words"]


def test_preview_other_dialect_does_not_adapt(rig, capsys):
    ctx = rig[0]
    calls = []
    ctx.provider.tag_adapter = lambda text, family: calls.append(family) or text.replace("(laughs)", "[laughs]")
    score = "(laughs) It passed."
    result = preview_without_side_effects(rig, score=score, dialect="eleven_v4")
    assert calls == []
    args = cli.add_commands(argparse.ArgumentParser()).parse_args(
        ["render", "--json", "--dialect", "eleven_v4", score])
    assert cli.run(args) == 0
    expected = json.loads(capsys.readouterr().out)
    assert result == dict(dialect="eleven_v4", rendered=expected["text"], cue_words=[],
                          dropped=[dict(kind=d["kind"], raw=d["value"], reason=d["reason"])
                                   for d in expected["dropped"]],
                          params=expected["params"], notes=expected["notes"], chars=len(expected["text"]))


@pytest.mark.parametrize("missing", [False, True])
def test_preview_fish_adapter_failure_matches_provider(rig, monkeypatch, missing):
    ctx = rig[0]

    def unavailable(*args):
        raise RuntimeError("fixture adapter unavailable")

    ctx.provider.tag_adapter = None if missing else unavailable
    if missing:
        monkeypatch.setattr(provider_module, "fish_adapter", unavailable)
    score = "(laughs) It passed."
    result = preview_without_side_effects(rig, score=score)
    assert result["rendered"] == score and result["cue_words"] == []
    assert result["notes"] == ["Fish adapt_tags unavailable; skipped pre-parse adaptation"]
    rendered, _, _ = ctx.provider._prepare(score, None, None)
    assert_preview_matches_render(result, rendered)


def test_preview_fish_uses_loaded_adapter_without_provider(rig, monkeypatch):
    from emotion_chip.hermes.tools import register_tools

    ctx = rig[0]
    fish = provider_module._fish()
    resolved = []

    def factory(loaded):
        resolved.append(loaded)
        return lambda text, family: text.replace("(laughs)", "[laughs]")

    monkeypatch.setattr(provider_module, "fish_adapter", factory)
    # Existing direct callers need not supply the newly optional provider.
    register_tools(ctx, ctx.provider.settings, ctx.provider.situation, ctx.provider.env)
    result = preview_without_side_effects(rig, score="(laughs) It passed.")
    assert resolved == [fish] and result["cue_words"] == []
    assert result["rendered"] == "[laughing] It passed."
    rendered, _, _ = ctx.provider._prepare("[laughs] It passed.", None, None)
    assert_preview_matches_render(result, rendered)


def test_preview_fish_includes_words_added_after_render(rig):
    ctx = rig[0]
    ctx.provider.tag_adapter = fish_like_adapt_tags
    score = "[[register: steady]] [warm] Hi. [speaker: 2] (happy) Bye."
    result = preview_without_side_effects(rig, score=score)
    rendered, _, _ = ctx.provider._prepare(score, None, None)
    assert_preview_matches_render(result, rendered)
    assert result["cue_words"] == provider_module.last_render["tools-session"]["cue_words"] == ["steady", "warm", "happy"]


def test_preview_eleven_never_calls_fish_adapter_or_consults_carry(rig):
    from emotion_chip.render import render

    ctx = rig[0]
    ctx.settings["backend"] = "eleven"
    calls = []
    ctx.provider.tag_adapter = lambda text, family: calls.append(family) or text.replace("(laughs)", "[laughs]")
    ctx.provider._carried["tools-session"] = ("not-a-group", 10000.0)
    score = "(laughs) It passed."
    result = preview_without_side_effects(rig, score=score)
    assert calls == [] and result["cue_words"] == []
    assert_preview_matches_render(result, render(score, "eleven_v4"))
    ctx.settings["backend"] = "fish"
    result = preview_without_side_effects(rig, score="It passed.")
    assert_preview_matches_render(result, render("It passed.", "fish_s2"))


@pytest.mark.parametrize("args", [{}, {"score": 1}, {"score": "x" * 10001},
                                  {"score": "Hi", "dialect": "unknown"}])
def test_preview_errors_are_json(rig, args):
    assert set(json.loads(rig[0].tools["chip_preview"]["handler"](args))) == {"error"}


def test_preview_limit_and_current_dialect(rig):
    ctx, host, _, _, _ = rig
    assert call(ctx, "chip_preview", score="x" * 10000)["chars"] == 10000
    host["tts.model"] = "drama-3"
    host.pop("tts.fish-audio.model")
    assert call(ctx, "chip_preview", score="Hi")["dialect"] == call(ctx, "chip_status")["dialect"] == "fish_drama3"


@pytest.mark.parametrize("signals", ["off", "facts", "facts+affordance"])
def test_speech_report_preserves_json_and_metadata(rig, signals):
    ctx, _, _, now, _ = rig
    ctx.settings["signals"] = signals
    prepare(ctx)
    row = deepcopy(provider_module.last_render["tools-session"])
    assert row["ts"] == now[0]
    original = ' {\n  "success" : true, "amount": 1.00, "clip": "fixture"\n}'
    result = speech(ctx, original)
    assert result.startswith(original.rstrip()[:-1].rstrip() + ', "emotion_chip": ')
    parsed = json.loads(result)
    assert list(parsed) == ["success", "amount", "clip", "emotion_chip"]
    assert parsed["emotion_chip"] == dict(dialect="eleven_v4", cue_words=["warm"],
        dropped=[dict(kind=d["kind"], raw=d["value"], reason=d["reason"]) for d in row["dropped"]], notes=row["notes"])
    assert provider_module.last_render["tools-session"] == row
    why = ctx.command("why")
    assert "ts" not in why and "authored cue words: warm" in why
    assert speech(ctx, result) is None


@pytest.mark.parametrize("reason", ["error", "provider", "defer", "old", "future", "other_session", "missing_row",
                                    "missing_ts", "not_json", "array", "scalar", "null", "non_string", "existing"])
def test_speech_report_skip(rig, reason):
    ctx, host, _, now, _ = rig
    prepare(ctx)
    kwargs, result = {}, '{"success": true}'
    if reason == "error":
        kwargs["status"] = "error"
    elif reason == "provider":
        host["tts.provider"] = "edge"
    elif reason == "defer":
        ctx.settings["guide_mode"] = "defer"
    elif reason == "old":
        now[0] += 120.01
    elif reason == "future":
        now[0] -= 1
    elif reason == "other_session":
        kwargs["session_id"] = "other"
    elif reason == "missing_row":
        provider_module.last_render.clear()
    elif reason == "missing_ts":
        provider_module.last_render["tools-session"].pop("ts")
    else:
        result = {"not_json": "oops", "array": "[]", "scalar": "1", "null": "null", "non_string": None,
                  "existing": '{"emotion_chip": {}}'}[reason]
    assert speech(ctx, result, **kwargs) is None


def test_speech_report_age_boundary_and_session_fallback(rig):
    ctx, _, session, now, _ = rig
    prepare(ctx)
    now[0] += 120
    assert json.loads(speech(ctx, "{}", session_id=""))["emotion_chip"]["cue_words"] == ["warm"]
    session["HERMES_SESSION_ID"] = "other"
    assert speech(ctx, "{}", session_id="") is None


def test_preview_null_dialect_uses_profile_dialect(rig):
    handler = rig[0].tools["chip_preview"]["handler"]
    assert json.loads(handler({"score": "[warm] Hi.", "dialect": None})) == json.loads(handler({"score": "[warm] Hi."}))


def test_speech_report_never_names_a_media_attachment(rig):
    import re
    ctx = rig[0]
    prepare(ctx, "[MEDIA:/tmp/secret.txt] Hello. [media: /tmp/other.ogg] Bye.")
    out = speech(ctx)
    report = json.loads(out)["emotion_chip"]
    assert any("media:/tmp/secret.txt" in word for word in report["cue_words"])  # the parsed value is unchanged
    tail = out[out.index('"emotion_chip"'):]
    assert re.search(r"media:", tail, re.IGNORECASE) is None  # Hermes's MEDIA: scan finds nothing in the report


def test_speech_report_cap(rig):
    ctx = rig[0]
    prepare(ctx, "[warm] " + "[speaker: 2] " * 100)
    before = deepcopy(provider_module.last_render)
    report = json.loads(speech(ctx))["emotion_chip"]
    assert len(json.dumps(report)) <= 600 and report["truncated"] is True
    assert len(report["dropped"]) < 100
    assert provider_module.last_render == before
    row = provider_module.last_render["tools-session"]
    row.update(cue_words=["暖" * 1000], dropped=[], notes=["note" * 500])
    report = json.loads(speech(ctx))["emotion_chip"]
    assert len(json.dumps(report)) <= 600 and report["truncated"] is True


def test_speech_report_keeps_notes_and_authored_header(rig):
    ctx = rig[0]
    prepare(ctx, "[[register: steady]] [warm] Hello.")
    report = json.loads(speech(ctx))["emotion_chip"]
    assert report["cue_words"] == ["steady", "warm"]
    assert report["notes"] == ["register header sets params; the first sentence's own cue leads"]


@pytest.mark.parametrize("signals", ["off", "facts", "facts+affordance"])
def test_other_tools_keep_signals_behavior(rig, signals):
    ctx = rig[0]
    ctx.settings["signals"] = signals
    prepare(ctx)
    transform = ctx.hooks["transform_tool_result"]
    original = '{"error": "fixture"}'
    result = transform(tool_name="terminal", status="error", result=original, session_id="tools-session", tool_call_id="c1")
    if signals == "off":
        assert result is None
    else:
        assert list(json.loads(result)) == ["error", "situation"]
    assert transform(tool_name="terminal", status="ok", result='{"success": true}', session_id="tools-session") is None
