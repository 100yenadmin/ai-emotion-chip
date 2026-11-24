"""Voice replies: Hermes's own decision, recorded at pre_gateway_dispatch, becomes one line for that message's turn."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from emotion_chip.hermes.hooks import Hooks
from emotion_chip.hermes.settings import Settings
from emotion_chip.hermes.situation import Situation
from emotion_chip.hermes.voice import FACT, MAX_PENDING, TTL_SECONDS, VoiceReplies, voice_fact
from fakes import FakeContext


def settings(host=None, **plugin):
    host = {"tts.provider": "emotion-chip", **(host or {})}
    return Settings(FakeContext(plugin), lambda key, default=None: host.get(key, default))


def event(platform="telegram", chat="42", message="7"):
    source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat, message_id=message)
    return SimpleNamespace(source=source, message_id=message)


class Gateway:
    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    def _should_send_voice_reply(self, event, response, agent_messages, already_sent=False):
        self.calls.append((response, agent_messages, already_sent))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def env(platform="telegram", chat="42", message="7"):
    values = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat, "HERMES_SESSION_MESSAGE_ID": message}
    return lambda key, default="": values.get(key, default)


def test_records_hermes_decision_for_that_message():
    replies, gateway = VoiceReplies(clock=lambda: 100.0), Gateway(True)
    replies.observe(event(), gateway)
    assert gateway.calls == [("reply", [], True)]          # no agent messages, so Hermes's own dedup does not apply
    assert replies.voiced(env(message="8")) is False        # another message's turn
    assert replies.voiced(env()) is True
    assert replies.voiced(env()) is False                   # used once: a queued follow-up keeps this message's id
    assert replies.voiced(env(chat="43")) is False
    replies.observe(event(message="8"), Gateway(False))
    assert replies.voiced(env(message="8")) is False


@pytest.mark.parametrize("gateway", [Gateway(RuntimeError("bug")), SimpleNamespace(), None, Gateway("yes")])
def test_unknown_means_no_line(gateway):
    replies = VoiceReplies()
    replies.observe(event(), gateway)
    assert replies.voiced(env()) is False


class DiscordAdapter:
    def __init__(self, in_vc=False):
        self.in_vc, self.asked = in_vc, []

    def play_in_voice_channel(self, guild_id, path):
        return True

    def is_in_voice_channel(self, guild_id):
        self.asked.append(guild_id)
        if isinstance(self.in_vc, Exception):
            raise self.in_vc
        return self.in_vc


class DiscordGateway(Gateway):
    """Hermes's runner as ``_deliver_voice_reply`` uses it: the chat's adapter and the message's guild."""

    def __init__(self, adapter, guild=123, answer=True):
        super().__init__(answer)
        self.adapter, self.guild = adapter, guild

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _get_guild_id(self, event):
        if isinstance(self.guild, Exception):
            raise self.guild
        return self.guild


def test_discord_text_channel_gets_the_line():
    replies, adapter = VoiceReplies(), DiscordAdapter(in_vc=False)
    gateway = DiscordGateway(adapter)
    replies.observe(event(platform="discord"), gateway)
    assert adapter.asked == [123] and gateway.calls == [("reply", [], True)]
    assert replies.voiced(env(platform="discord")) is True and replies.check == "ok"


def test_discord_voice_channel_gets_no_line():
    replies, gateway = VoiceReplies(), DiscordGateway(DiscordAdapter(in_vc=True))
    replies.observe(event(platform="discord"), gateway)
    assert gateway.calls == []                              # Hermes plays its own reply in the channel
    assert replies.voiced(env(platform="discord")) is False and replies.check is None


@pytest.mark.parametrize("guild", [None, 0])
def test_discord_dm_gets_the_line(guild):
    replies, adapter = VoiceReplies(), DiscordAdapter(in_vc=True)
    replies.observe(event(platform="discord"), DiscordGateway(adapter, guild=guild))
    assert adapter.asked == []                              # no guild: Hermes sends a voice message, never plays
    assert replies.voiced(env(platform="discord")) is True


def test_discord_adapter_without_channel_playback_gets_the_line():
    replies = VoiceReplies()
    replies.observe(event(platform="discord"), DiscordGateway(SimpleNamespace(is_in_voice_channel=lambda g: True)))
    assert replies.voiced(env(platform="discord")) is True  # no play_in_voice_channel: Hermes sends a voice message


@pytest.mark.parametrize("gateway", [
    Gateway(True),                                          # no _delivery_adapter_for or _get_guild_id
    DiscordGateway(DiscordAdapter(in_vc=RuntimeError("bug"))),
    DiscordGateway(DiscordAdapter(), guild=RuntimeError("bug")),
])
def test_discord_unknown_means_no_line(gateway):
    replies = VoiceReplies()
    replies.observe(event(platform="discord"), gateway)
    assert replies.voiced(env(platform="discord")) is False and gateway.calls == []


def test_stale_and_id_less_decisions():
    now = [0.0]
    replies = VoiceReplies(clock=lambda: now[0])
    replies.observe(event(), Gateway(True))
    now[0] = TTL_SECONDS + 1
    assert replies.voiced(env()) is False
    replies.observe(event(message=""), Gateway(True))
    assert replies.voiced(env(message="")) is True
    assert replies.voiced(env(message="")) is False
    replies.observe(event(message=""), Gateway(True))
    assert replies.voiced(env(message="7")) is False          # one side has an id: no match


def test_bounded_and_missing_source():
    replies = VoiceReplies()
    for n in range(MAX_PENDING + 10):
        replies.observe(event(chat=str(n)), Gateway(True))
    assert len(replies._pending) == MAX_PENDING and replies.voiced(env(chat=str(MAX_PENDING + 9), message="7"))
    assert replies.voiced(env(chat="0", message="7")) is False   # the oldest were dropped
    replies.observe(SimpleNamespace(source=None), Gateway(True))
    assert len(replies._pending) == MAX_PENDING - 1             # one was used; a sourceless event adds nothing


def test_fact_only_with_the_chip_guide():
    replies = VoiceReplies()
    replies.observe(event(), Gateway(True))
    assert voice_fact(settings(), replies, env()) == FACT and "text_to_speech" in FACT
    assert voice_fact(settings(guide_mode="defer"), replies, env()) == ""
    assert voice_fact(settings({"tts.provider": "elevenlabs"}), replies, env()) == ""


def test_hooks():
    hooks = Hooks(settings(), Situation({}, lambda: 1000.0), env())
    assert hooks.pre_gateway_dispatch(event=event(), gateway=Gateway(True), session_store=None) is None
    assert hooks.pre_llm_call(session_id="s1", platform="telegram") == {"context": FACT}
    assert hooks.pre_llm_call(session_id="s2", platform="telegram", parent_session_id="s1") is None
    assert hooks.pre_gateway_dispatch(event=object(), gateway=Gateway(RuntimeError())) is None
    hooks = Hooks(settings(signals="facts"), Situation({}, lambda: 1000.0), env())
    hooks.pre_gateway_dispatch(event=event(), gateway=Gateway(True))
    hooks.situation.update("s3", lambda row: row.update(tool_error_streak=3))
    assert hooks.pre_llm_call(session_id="s3", platform="telegram")["context"].endswith("\n" + FACT)


def test_hermes_voice_contract():
    root = Path(os.environ.get("HERMES_AGENT_SRC") or Path.home() / ".hermes" / "hermes-agent")
    if not root.is_dir():
        pytest.skip("Hermes source absent")
    voice = (root / "gateway" / "run_voice.py").read_text()
    assert ("def _should_send_voice_reply(\n        self, event: MessageEvent, response: str, agent_messages: list, "
            "already_sent: bool = False\n    ) -> bool:") in voice
    assert ('(tc.get("function") or {}).get("name") == "text_to_speech"' in voice  # the tool-call dedup (<= v0.23.1)
            or '"text_to_speech" in tool_name_by_call_id(agent_messages[start:]).values()' in voice)  # v0.23.2+
    assert "return not (is_voice_input and not already_sent)" in voice
    # Discord: the voice-channel test the chip mirrors, and the two runner methods it calls.
    assert "def _get_guild_id(event: MessageEvent) -> Optional[int]:" in voice
    assert "if guild_id and callable(play) and callable(is_in_vc) and is_in_vc(guild_id):" in voice
    assert 'play = getattr(adapter, "play_in_voice_channel", None)' in voice
    assert "def _delivery_adapter_for(self, source" in (root / "gateway" / "authz_mixin.py").read_text()
    hooks = (root / "gateway" / "run_inbound_hooks.py").read_text()
    assert '"pre_gateway_dispatch", event=event, gateway=self,' in hooks
    run = (root / "gateway" / "run.py").read_text()
    assert "message_id=str(source.message_id) if source.message_id else \"\"," in run
    assert "def _append_auto_media_tags" in (root / "gateway" / "run_turn_runner.py").read_text()


def test_other_messages_do_not_overwrite_and_stale_entries_go():
    now = [0.0]
    replies = VoiceReplies(clock=lambda: now[0])
    replies.observe(event(message="7"), Gateway(True))
    replies.observe(event(message="8"), Gateway(False))      # another sender in the group, before turn 7 runs
    assert replies.voiced(env(message="7")) is True
    now[0] = TTL_SECONDS + 5
    replies.observe(event(message="9"), Gateway(True))
    assert list(replies._pending) == [("telegram", "42", "9")]


def test_dispatch_hook_registered_only_for_the_voice_line(tmp_path):
    from emotion_chip.hermes.registration import register_all
    for host, plugin, expected in (({"tts.provider": "emotion-chip"}, {}, True),
                                   ({"tts.provider": "edge"}, {}, False),
                                   ({"tts.provider": "emotion-chip"}, {"guide_mode": "defer"}, False)):
        ctx = FakeContext(plugin)
        register_all(ctx, reader=lambda key, default=None, host=host: host.get(key, default),
                     data_dir=lambda: tmp_path / "data")
        assert ("pre_gateway_dispatch" in ctx.hooks) is expected


def test_identity_is_the_exported_source_message_id():
    # BlueBubbles sets event.message_id but not source.message_id; the turn exports an empty id.
    replies = VoiceReplies()
    ev = event(message="")
    ev.message_id = "evt-1"
    replies.observe(ev, Gateway(True))
    assert replies.voiced(env(message="evt-1")) is False
    replies.observe(ev, Gateway(True))
    assert replies.voiced(env(message="")) is True
