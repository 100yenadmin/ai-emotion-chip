"""Offline agent tools and the bounded speech-result report."""
from copy import deepcopy
import argparse
import json
from types import SimpleNamespace
import sys

import pytest

from emotion_chip import cli
from emotion_chip.data import load_data
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
                         "signals", "quiet", "delivery_log", "voice_replies", "session_id", "register"} | (
                             set() if case == "no" else {"eleven_voice_id" if case == "eleven" else "fish_model"})
    assert data["session_id"] == "tools-session" and data["register"] == ""
    assert data["quiet"] is False and data["delivery_log"] is True
    ready = data["ready"]
    if data["problems"]:
        ready += " (" + "; ".join(data["problems"]) + ")"
    elif data["not_checked"]:
        ready += " (not checked from here: " + ", ".join(data["not_checked"]) + ")"
    fields = dict(ready=ready, backend=data["backend"])
    if case != "eleven":
        if case != "no":
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


def assert_preview_matches_provider(rig, score, **args):
    result = preview_without_side_effects(rig, score=score, **args)
    # Hermes calls a plugin provider with tts.model and tts.speed (None when unset).
    host = rig[1]
    rendered, _, _ = rig[0].provider._prepare(score, host.get("tts.model") or None, host.get("tts.speed"))
    assert_preview_matches_render(result, rendered)
    assert result["cue_words"] == provider_module.last_render["tools-session"]["cue_words"]
    return result


@pytest.mark.parametrize("speed", [1.5, 10.0])
def test_preview_fish_host_speed_matches_provider(rig, speed):
    from emotion_chip.render import render

    rig[1]["tts.fish-audio.speed"] = speed
    score = "[sad] Sorry."
    group_speed = render(score, "fish_s2").params["speed"]
    assert group_speed != 1.0
    result = assert_preview_matches_provider(rig, score)
    assert result["params"]["speed"] == max(0.5, min(2.0, speed * group_speed))


def test_preview_fish_s1_dialect_notes_match_provider(rig):
    ctx, host, _, _, _ = rig
    host["tts.model"] = "s1"
    host.pop("tts.fish-audio.model")
    result = assert_preview_matches_provider(rig, "[warm] Hello.")
    assert result["dialect"] == "tagless" == call(ctx, "chip_status")["dialect"]
    assert "fish s1 takes fixed cues only; rendering tagless" in result["notes"]


def test_preview_fish_hermes_call_speed_matches_provider(rig):
    from emotion_chip.render import render

    rig[1]["tts.speed"] = 1.25
    score = "[sad] Sorry."
    group_speed = render(score, "fish_s2").params["speed"]
    result = assert_preview_matches_provider(rig, score)
    assert result["params"]["speed"] == max(0.5, min(2.0, 1.25 * group_speed))


def test_preview_fish_pinned_model_note_matches_provider(rig):
    rig[0].settings["fish_model"] = "s1"
    result = assert_preview_matches_provider(rig, "[warm] Hello.")
    assert "Fish pinned model wins: s2.1-pro over requested s1" in result["notes"]


def test_preview_eleven_overrides_match_provider(rig):
    rig[0].settings.update(backend="eleven", eleven_style=0.75, eleven_speed=1.2)
    result = assert_preview_matches_provider(rig, "[warm] Hello.")
    assert result["params"]["style"] == 0.75
    assert result["params"]["speed"] == 1.2


def test_preview_fish_after_render_adapter_failure_matches_provider(rig):
    calls = []

    def adapter(text, family):
        calls.append(text)
        if len(calls) > 2:
            raise RuntimeError("fixture adapter fails after pre-parse adaptation")
        return text.replace("(laughs)", "[laughs]")

    ctx = rig[0]
    ctx.provider.tag_adapter = adapter
    score = "(laughs) Hi. [pause] (laughs) Sorry."
    result = preview_without_side_effects(rig, score=score)
    assert len(calls) == 3
    assert "Fish adapt_tags failed after render; output not re-checked" in result["notes"]
    calls.clear()
    rendered, _, _ = ctx.provider._prepare(score, None, None)
    assert_preview_matches_render(result, rendered)
    assert result["cue_words"] == provider_module.last_render["tools-session"]["cue_words"]


def test_provider_preview_skips_eleven_length_check(rig):
    ctx = rig[0]
    ctx.settings.update(backend="eleven", eleven_model="eleven_v3")
    score = "x" * 5001
    result = preview_without_side_effects(rig, score=score)
    assert result["chars"] == 5001
    with pytest.raises(ValueError, match="exceeds 5000"):
        ctx.provider._prepare(score, None, None)


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


COMPAT_CASES = [("edge", None, None, "tagless")] + [
    ("fish-audio", model, chip_model, dialect)
    for model, dialect in ((None, "fish_s2"), ("s1", "tagless"), ("s2.1-pro", "fish_s2"), ("drama-3", "fish_drama3"))
    for chip_model in (None, "s1")
] + [("elevenlabs", "eleven_v4", None, "eleven_v4"),
     ("elevenlabs", "eleven_multilingual_v2", None, "tagless")]
COMPAT_SCORES = ["[whisper] The tests passed. [pause] The baby is still asleep.",
                 "(laughs) Hi there.", "Hello.", "[soft: it worked], really."]


def register_compat(tmp_path, monkeypatch, provider, model=None, chip_model=None, backend="fish"):
    from emotion_chip.hermes import middleware

    ctx = FakeContext({"backend": backend, **({"fish_model": chip_model} if chip_model else {})})
    host = {"tts.provider": provider}
    if model is not None:
        host["tts.elevenlabs.model_id" if provider == "elevenlabs" else "tts.model"] = model
    monkeypatch.setattr(provider_module, "_fish", lambda: FakeFish())
    monkeypatch.setattr(middleware, "fish_adapter", lambda fish: fish_like_adapt_tags)
    register_all(ctx, reader=lambda key, default=None: host.get(key, default),
                 data_dir=lambda: tmp_path, env=lambda key: "")
    return ctx, host


@pytest.mark.parametrize("provider,model,chip_model,dialect", COMPAT_CASES)
@pytest.mark.parametrize("score", COMPAT_SCORES)
@pytest.mark.parametrize("explicit", ["absent", "null", "target"])
def test_compat_preview_status_skill_match_middleware(tmp_path, monkeypatch, provider, model, chip_model,
                                                    dialect, score, explicit):
    from emotion_chip.guide import build_skill

    ctx, _ = register_compat(tmp_path, monkeypatch, provider, model, chip_model)
    args = {"text": score, "output_path": "fixture.mp3"}
    before = deepcopy(ctx.state)
    passed = ctx.middleware(tool_name="text_to_speech", args=args)
    expected = score if passed is None else passed["args"]["text"]
    options = {} if explicit == "absent" else {"dialect": None if explicit == "null" else dialect}
    preview = call(ctx, "chip_preview", score=score, **options)
    assert preview["rendered"] == expected and preview["chars"] == len(expected)
    assert set(preview) == {"dialect", "rendered", "cue_words", "dropped", "params", "notes", "chars"}
    status = call(ctx, "chip_status")
    assert preview["dialect"] == status["dialect"] == dialect
    assert ctx.skill[0].read_text() == build_skill(dialect, "default", load_data()) + "\n"
    assert ctx.section({}) == ""  # Compat still adds no system-prompt section.
    assert f"dialect: {dialect}" in ctx.command("status")
    assert "eleven_voice_id" not in status
    if provider == "fish-audio":
        assert status["fish_model"] == (model or "fish-default")
    else:
        assert "fish_model" not in status and "fish_model:" not in ctx.command("status")
    if passed is None:
        assert preview["cue_words"] == preview["dropped"] == []
        assert any("sent unchanged" in note for note in preview["notes"])
    elif score.startswith("[whisper]"):
        assert preview["cue_words"] == ["whisper"]
    assert args == {"text": score, "output_path": "fixture.mp3"} and ctx.state == before
    assert not (tmp_path / "delivery.jsonl").exists()


@pytest.mark.parametrize("stage", ["lookup", "before", "after"])
@pytest.mark.parametrize("score", ["(laughs) Hi there.", "[warm] Hi. [speaker: 2] (happy) Bye."])
def test_compat_preview_fish_adapter_failure_matches_middleware(tmp_path, monkeypatch, stage, score):
    from emotion_chip.hermes import middleware

    ctx, _ = register_compat(tmp_path, monkeypatch, "fish-audio")
    calls = []

    def adapt(text, family):
        calls.append(text)
        if stage == "before" or len(calls) > 2:
            raise RuntimeError("fixture adapter failure")
        return text.replace("(laughs)", "[laughs]")

    def factory(fish):
        if stage == "lookup":
            raise RuntimeError("fixture adapter unavailable")
        return adapt

    monkeypatch.setattr(middleware, "fish_adapter", factory)
    passed = ctx.middleware(tool_name="text_to_speech", args={"text": score})
    calls.clear()
    preview = call(ctx, "chip_preview", score=score)
    assert preview["rendered"] == (score if passed is None else passed["args"]["text"])
    if stage == "after":
        assert "Fish adapt_tags failed after render; output not re-checked" in preview["notes"]


@pytest.mark.parametrize("provider,model", [("edge", None), ("fish-audio", None), ("elevenlabs", "eleven_v4")])
def test_compat_status_omits_chip_voice_with_eleven_backend(tmp_path, monkeypatch, provider, model):
    ctx, _ = register_compat(tmp_path, monkeypatch, provider, model, "s1", backend="eleven")
    status = call(ctx, "chip_status")
    assert "eleven_voice_id" not in status and "eleven_voice_id:" not in ctx.command("status")
    assert ("fish_model" in status) == (provider == "fish-audio")
    if provider == "fish-audio":
        assert status["fish_model"] == "fish-default"
        assert "fish_model: fish-default" in ctx.command("status")


def test_compat_explicit_other_dialect_keeps_plain_render(tmp_path, monkeypatch):
    from emotion_chip.render import render

    ctx, _ = register_compat(tmp_path, monkeypatch, "edge")
    ctx.provider.tag_adapter = lambda text, family: text
    score = COMPAT_SCORES[0]
    result = call(ctx, "chip_preview", score=score, dialect="fish_s2")
    assert_preview_matches_render(result, render(score, "fish_s2"))
    assert result["cue_words"] == ["whisper"]


@pytest.mark.parametrize("quiet", [False, True])
def test_compat_preview_keeps_later_fish_words_and_quiet(tmp_path, monkeypatch, quiet):
    ctx, _ = register_compat(tmp_path, monkeypatch, "fish-audio")
    if quiet:
        ctx.command("quiet on")
    score = "[[register: steady]] [warm] Hi. [speaker: 2] (happy) Bye. [sigh]"
    passed = ctx.middleware(tool_name="text_to_speech", args={"text": score})
    preview = call(ctx, "chip_preview", score=score)
    assert preview["rendered"] == passed["args"]["text"]
    assert preview["cue_words"] == ["steady", "warm", "happy"]
    assert ("[sighing]" in preview["rendered"]) is not quiet


def test_compat_preview_render_failure_sends_original(tmp_path, monkeypatch):
    from emotion_chip.hermes import middleware

    ctx, _ = register_compat(tmp_path, monkeypatch, "fish-audio")

    def broken(*args):
        raise RuntimeError("fixture render failure")

    monkeypatch.setattr(middleware, "settled_render", broken)
    score = "(laughs) Hi. [warm] Bye."
    assert ctx.middleware(tool_name="text_to_speech", args={"text": score}) is None
    preview = call(ctx, "chip_preview", score=score)
    assert preview["rendered"] == score and preview["cue_words"] == preview["dropped"] == []
    assert any("sent unchanged" in note for note in preview["notes"])


def test_compat_skill_tracks_provider_changes(tmp_path, monkeypatch):
    from emotion_chip.guide import build_skill

    ctx, host = register_compat(tmp_path, monkeypatch, "edge")
    for provider, model, dialect in (("fish-audio", "drama-3", "fish_drama3"),
                                     ("elevenlabs", "eleven_v4", "eleven_v4"), ("edge", None, "tagless")):
        host.update({"tts.provider": provider, "tts.model": model, "tts.elevenlabs.model_id": model})
        assert ctx.section({}) == ""
        assert ctx.skill[0].read_text() == build_skill(dialect, "default", load_data()) + "\n"
        assert call(ctx, "chip_preview", score="Hello.")["dialect"] == call(ctx, "chip_status")["dialect"] == dialect


@pytest.mark.parametrize("backend,dialect", [("fish", "fish_s2"), ("eleven", "eleven_v4")])
def test_chip_provider_skill_status_preview_stay_aligned(rig, backend, dialect):
    from emotion_chip.guide import build_skill

    ctx = rig[0]
    ctx.settings["backend"] = backend
    assert ctx.section({})  # Refresh the skill after a backend change.
    assert ctx.skill[0].read_text() == build_skill(dialect, "default", load_data()) + "\n"
    assert call(ctx, "chip_preview", score="[warm] Hi.")["dialect"] == call(ctx, "chip_status")["dialect"] == dialect
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[warm] Hi."}) is None


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


@pytest.mark.parametrize("model_id", [None, 7, ""])
def test_elevenlabs_compat_with_unset_model_registers_and_renders_tagless(model_id, tmp_path):
    from emotion_chip.guide import build_skill
    ctx = FakeContext({})
    host = {"tts.provider": "elevenlabs", "tts.elevenlabs.model_id": model_id}
    register_all(ctx, reader=lambda name, default=None: host.get(name, default), data_dir=lambda: tmp_path)
    score = "[whisper] The tests passed. [pause] The baby is still asleep."
    passed = ctx.middleware(tool_name="text_to_speech", args={"text": score})
    expected = passed["args"]["text"] if passed else score
    preview = json.loads(ctx.tools["chip_preview"]["handler"]({"score": score}))
    assert preview["dialect"] == "tagless" and preview["rendered"] == expected
    assert "[" not in expected
    assert json.loads(ctx.tools["chip_status"]["handler"]({}))["dialect"] == "tagless"
    assert ctx.skill[0].read_text() == build_skill("tagless", "default", load_data()) + "\n"

