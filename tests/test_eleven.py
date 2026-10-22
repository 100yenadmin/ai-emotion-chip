"""Offline ElevenLabs transport and provider paths."""
from io import BytesIO
import json
from urllib.error import HTTPError
import wave

import pytest

from emotion_chip.data import load_data
from emotion_chip.guide import build_prompt_section, build_skill
from emotion_chip.hermes import eleven as eleven_module
from emotion_chip.hermes import provider as provider_module
from emotion_chip.hermes.eleven import ElevenClient
from emotion_chip.hermes.provider import EmotionChipTTSProvider
from emotion_chip.hermes.registration import register_all
from emotion_chip.hermes.settings import ELEVEN_MODELS
from emotion_chip.render import render
from fakes import FakeContext


class Response(BytesIO):
    def __init__(self, body=b"\x01\x00" * 5000, request_id="fixture-request"):
        super().__init__(body)
        self.headers = {"request-id": request_id}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-eleven-key")
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response(request_id=f"request-{len(calls)}")

    monkeypatch.setattr(eleven_module, "urlopen", opener)
    monkeypatch.setattr(provider_module, "_fish", lambda: pytest.fail("Eleven must not resolve Fish"))
    ctx = FakeContext({"backend": "eleven", "eleven_voice_id": "setting-voice"})
    host = {"tts.provider": "emotion-chip"}
    session = {"HERMES_SESSION_ID": "fixture-session", "HERMES_SESSION_PLATFORM": "telegram"}
    now = [10000.0]
    register_all(ctx, reader=lambda key, default=None: host.get(key, default), data_dir=lambda: tmp_path,
                 env=lambda key: session.get(key, ""), clock=lambda: now[0])
    return ctx, host, session, now, calls, tmp_path


def payload(calls, index=-1):
    return json.loads(calls[index][0].data)


@pytest.mark.parametrize("model", ELEVEN_MODELS)
def test_dialect_render_section_skill_and_status(rig, model):
    ctx, host, _, _, calls, directory = rig
    host["tts.elevenlabs.model_id"] = model
    register_all(ctx, reader=ctx.provider.settings.reader, data_dir=lambda: directory,
                 env=ctx.provider.env, clock=ctx.provider.clock)
    text = "[soft: Hello.] [sigh]"
    ctx.provider.synthesize(text, directory / "out.mp3")
    assert payload(calls)["text"] == "[softly] Hello. [sighs]"
    assert payload(calls)["model_id"] == model
    assert ctx.section({}) == build_prompt_section("eleven_v4", "default", load_data())
    assert ctx.skill[0].read_text() == build_skill("eleven_v4", "default", load_data()) + "\n"
    assert f"eleven_model: {model}" in ctx.command("status")
    assert "eleven_voice_id: setting-voice" in ctx.command("status")
    assert "dialect: eleven_v4" in ctx.command("status")
    assert f"ElevenLabs model: {model}" in ctx.command("why")
    row = json.loads((directory / "delivery.jsonl").read_text())
    assert row["dialect"] == "eleven_v4" and row["chars"] == len(text)
    assert text not in (directory / "delivery.jsonl").read_text()


def test_synthesize_request_and_call_time_key(rig, monkeypatch):
    ctx, _, _, _, calls, directory = rig
    ctx.set_config("eleven_style", 0.30)
    ctx.set_config("eleven_speed", 0.90)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-replaced-key")
    path = directory / "out.mp3"
    text = "[sad] Again."
    assert ctx.provider.synthesize(text, path, voice="call voice/fixture", speed=1.8) == path
    request, timeout = calls[-1]
    assert request.full_url == "https://api.elevenlabs.io/v1/text-to-speech/call%20voice%2Ffixture?output_format=mp3_44100_128"
    assert request.get_method() == "POST" and timeout == 60
    assert dict((k.lower(), v) for k, v in request.header_items()) == {
        "xi-api-key": "fixture-replaced-key", "content-type": "application/json", "accept": "audio/*"}
    params = render(text, "eleven_v4").params
    assert payload(calls) == {"text": text, "model_id": "eleven_v4",
                              "voice_settings": dict(params, style=0.30, speed=0.90)}
    assert path.read_bytes() == b"\x01\x00" * 5000
    ctx.set_config("eleven_style", None)
    ctx.set_config("eleven_speed", None)
    ctx.provider.synthesize("Hi.", path)
    assert payload(calls)["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.75}
    ctx.set_config("eleven_style", 0)
    ctx.set_config("eleven_speed", 1)
    ctx.provider.synthesize("Hi.", path)
    assert payload(calls)["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.75, "style": 0, "speed": 1}


@pytest.mark.parametrize("format,query", [("mp3", "mp3_44100_128"), ("ogg", "opus_48000_64"),
    ("opus", "opus_48000_64"), ("pcm", "pcm_24000"), ("wav", "pcm_24000")])
def test_formats(rig, format, query):
    ctx, _, _, _, calls, directory = rig
    path = directory / ("out." + format)
    ctx.provider.synthesize("Hi.", path, format=format)
    assert calls[-1][0].full_url.endswith("?output_format=" + query)
    if format == "wav":
        with wave.open(str(path), "rb") as handle:
            assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (1, 2, 24000)
            assert handle.readframes(5000) == b"\x01\x00" * 5000
    else:
        assert path.read_bytes() == b"\x01\x00" * 5000


def test_unknown_format(rig):
    ctx, _, _, _, calls, directory = rig
    with pytest.raises(ValueError, match="Unknown ElevenLabs format"):
        ctx.provider.synthesize("Hi.", directory / "out.bad", format="bad")
    assert calls == [] and not (directory / "out.bad").exists()


def test_stream_chunks_and_capabilities(rig):
    ctx, _, _, _, calls, _ = rig
    chunks = list(ctx.provider.stream("Hi."))
    assert list(map(len, chunks)) == [4096, 4096, 1808]
    assert b"".join(chunks) == b"\x01\x00" * 5000
    assert calls[-1][0].full_url.endswith("/setting-voice/stream?output_format=pcm_24000")
    assert payload(calls) == {"text": "Hi.", "model_id": "eleven_v4",
                              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    p = ctx.provider
    assert p.streams_pcm and p.stream_sample_rate == 24000 and p.is_available()
    assert p.list_voices() == [] and p.default_voice() == "setting-voice"
    assert p.list_models() == [{"id": model} for model in ELEVEN_MODELS]
    assert p.warm() is None and p.release() is None


@pytest.mark.parametrize("model", ["eleven_v4", "eleven_v4_turbo"])
def test_stitch_ring_and_expiry(rig, model):
    ctx, _, _, now, calls, directory = rig
    ctx.set_config("eleven_model", model)
    p = ctx.provider
    for index in range(5):
        p.synthesize("Hi.", directory / "out.mp3")
        expected = [f"request-{i}" for i in range(max(1, index - 2), index + 1)]
        assert payload(calls).get("previous_request_ids", []) == expected
    assert len(p._request_ids["fixture-session"]) == 3
    now[0] += 7199
    p.synthesize("Hi.", directory / "out.mp3")
    assert payload(calls)["previous_request_ids"] == ["request-3", "request-4", "request-5"]
    now[0] += 1
    p.synthesize("Hi.", directory / "out.mp3")
    assert payload(calls)["previous_request_ids"] == ["request-6"]
    now[0] += 7200
    p.synthesize("Hi.", directory / "out.mp3")
    assert "previous_request_ids" not in payload(calls)


def test_stitch_isolation_and_stream_exclusion(rig):
    ctx, _, session, _, calls, directory = rig
    p = ctx.provider
    p.synthesize("Hi.", directory / "out.mp3")
    list(p.stream("Stream."))
    assert "previous_request_ids" not in payload(calls)
    assert p._request_ids["fixture-session"] == [("request-1", 10000)]
    session["HERMES_SESSION_ID"] = "other-session"
    p.synthesize("Other.", directory / "out.mp3")
    assert "previous_request_ids" not in payload(calls)
    session["HERMES_SESSION_ID"] = "fixture-session"
    p.synthesize("Back.", directory / "out.mp3")
    assert payload(calls)["previous_request_ids"] == ["request-1"]
    session["HERMES_SESSION_ID"] = ""
    p.synthesize("Anonymous.", directory / "out.mp3")
    p.synthesize("Anonymous again.", directory / "out.mp3")
    assert "previous_request_ids" not in payload(calls)
    session["HERMES_SESSION_KEY"] = "fixture-key"
    p.synthesize("Key.", directory / "out.mp3")
    p.synthesize("Key again.", directory / "out.mp3")
    assert payload(calls)["previous_request_ids"] == ["request-7"]


def test_v3_never_stitches(rig):
    ctx, _, _, _, calls, directory = rig
    p = ctx.provider
    p.synthesize("Hi.", directory / "out.mp3")
    ctx.set_config("eleven_model", "eleven_v3")
    for _ in range(2):
        p.synthesize("V3.", directory / "out.mp3")
        assert "previous_request_ids" not in payload(calls)
    list(p.stream("V3 stream."))
    assert "previous_request_ids" not in payload(calls)
    assert p._request_ids["fixture-session"] == [("request-1", 10000)]


@pytest.mark.parametrize("model,limit", [("eleven_v4", 10000), ("eleven_v4_turbo", 5000), ("eleven_v3", 5000)])
@pytest.mark.parametrize("streaming", [False, True])
def test_rendered_character_caps(rig, model, limit, streaming):
    ctx, _, _, _, calls, directory = rig
    ctx.set_config("eleven_model", model)
    p = ctx.provider

    def speak(text):
        return list(p.stream(text)) if streaming else p.synthesize(text, directory / "out.mp3")

    assert p.max_text_length == limit
    speak("a" * limit)
    assert len(payload(calls)["text"]) == limit
    with pytest.raises(ValueError, match=f"{limit + 1} exceeds {limit}"):
        speak("a" * (limit + 1))
    assert len(calls) == 1
    # Caps apply after rendering: speaker markup is dropped, never silently truncating speech.
    speak("[speaker: 2] " + "a" * limit)
    assert payload(calls)["text"] == "a" * limit


@pytest.mark.parametrize("streaming", [False, True])
def test_http_errors_redact_key(rig, streaming):
    ctx, _, _, _, _, directory = rig

    def fail(request, timeout):
        raise HTTPError(request.full_url, 429, "fixture", {},
                        BytesIO(b"fixture-eleven-key: quota " + b"x" * 300))

    ctx.provider.eleven.opener = fail
    with pytest.raises(RuntimeError) as caught:
        if streaming:
            list(ctx.provider.stream("Hi."))
        else:
            ctx.provider.synthesize("Hi.", directory / "out.mp3")
    message = str(caught.value)
    assert message.startswith("ElevenLabs HTTP 429: [redacted]: quota ")
    assert len(message.split(": ", 1)[1]) == 200
    assert "fixture-eleven-key" not in message
    assert not (directory / "out.mp3").exists()
    assert ctx.provider._request_ids == {}


def test_injected_opener_and_timeout(rig):
    ctx, _, _, _, calls, directory = rig

    def opener(request, timeout):
        calls.append(timeout)
        return Response()

    p = EmotionChipTTSProvider(ctx.provider.settings, data_dir=lambda: directory, opener=opener, timeout=7)
    p.synthesize("Hi.", directory / "out.mp3")
    assert calls == [7]
    client = ElevenClient(opener=opener, timeout=3)
    assert list(client.stream("Hi.", voice="voice", model="eleven_v4", params={}))
    assert calls[-1] == 3


def test_voice_and_model_resolution(rig):
    ctx, host, _, _, calls, directory = rig
    p = ctx.provider
    host.update({"tts.elevenlabs.voice_id": "host-voice", "tts.elevenlabs.model_id": "eleven_v3"})
    ctx.set_config("eleven_model", "eleven_v4_turbo")
    p.synthesize("Hi.", directory / "out.mp3", voice="call-voice", model="eleven_v4")
    assert "/call-voice?" in calls[-1][0].full_url and payload(calls)["model_id"] == "eleven_v3"
    p.synthesize("Hi.", directory / "out.mp3")
    assert "/host-voice?" in calls[-1][0].full_url and p.default_voice() == "host-voice"
    host["tts.elevenlabs.voice_id"] = ""
    host["tts.elevenlabs.model_id"] = "unsupported-model"
    p.synthesize("Hi.", directory / "out.mp3")
    assert "/setting-voice?" in calls[-1][0].full_url and payload(calls)["model_id"] == "eleven_v4_turbo"


def test_missing_voice_or_key(rig, monkeypatch):
    ctx, _, _, _, calls, directory = rig
    p = ctx.provider
    ctx.set_config("eleven_voice_id", "")
    assert not p.is_available() and p.default_voice() == ""
    with pytest.raises(ValueError, match="voice id"):
        p.synthesize("Hi.", directory / "out.mp3")
    assert calls == []
    p.synthesize("Hi.", directory / "out.mp3", voice="call-voice")
    ctx.set_config("eleven_voice_id", "voice")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert not p.is_available()
    with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
        p.synthesize("Hi.", directory / "out.mp3")
    assert len(calls) == 1


def test_carry_quiet_parens_and_compat_unchanged(rig):
    ctx, host, session, now, calls, directory = rig
    p = ctx.provider
    p.tag_adapter = lambda *args: pytest.fail("Eleven must not adapt Fish parens")
    list(p.stream("[sad] (sobbing) One. [sigh]"))
    first = payload(calls)["voice_settings"]
    assert "(sobbing)" in payload(calls)["text"] and "[sighs]" in payload(calls)["text"]
    for _ in range(6):
        now[0] += 30
        list(p.stream("Two."))
        assert payload(calls)["voice_settings"] == first
    session["HERMES_SESSION_ID"] = "other-session"
    list(p.stream("Other."))
    assert payload(calls)["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.75}
    ctx.command("quiet on")
    p.synthesize("[sad] Hello. [sigh]", directory / "out.mp3")
    assert payload(calls)["text"] == "[sad] Hello."
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[soft: here]"}) is None
    host.update({"tts.provider": "elevenlabs", "tts.elevenlabs.model_id": "eleven_v4"})
    assert ctx.middleware(tool_name="text_to_speech", args={"text": "[soft: here]"}) == {
        "args": {"text": "[softly] here"}}
