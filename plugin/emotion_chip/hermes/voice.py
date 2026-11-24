"""Tell the agent when Hermes will read its reply aloud, so it can say the reply itself with its cues.

With voice replies on in a chat (/voice, or voice.auto_tts), Hermes speaks the reply text itself, without cues. When
the agent calls text_to_speech in the turn instead, Hermes sends no voice note of its own: the runner skips its voice
reply after a text_to_speech call (gateway/run_voice.py ``_should_send_voice_reply``), and the adapter's auto voice
reply needs a reply without media, which the tool's audio gives it (``_append_auto_media_tags``).

Whether Hermes will voice a reply is Hermes's own decision, asked at ingress in ``pre_gateway_dispatch``, which gets
the inbound event and, in process, the gateway runner: ``_should_send_voice_reply(event, …, already_sent=True)``
answers it for the chat's /voice mode (keyed by the bot that owns the chat), ``voice.auto_tts`` and the message's
type. The answer is kept in memory under the chat and message id, and ``pre_llm_call`` turns it into one line for that
message's turn, once. Anything unknown (an isolated plugin host, a missing method, an error) means no line, which is what
Hermes does without the chip.

Discord: Hermes plays its voice reply in a voice channel only while the bot is connected in that guild
(gateway/run_voice.py ``_deliver_voice_reply``); a clip from the agent would go to the text channel instead, so that
message gets no line. Otherwise (a text channel or thread with no voice connection in the guild, or a DM) Hermes sends
its reply to the chat as a voice message, as on Telegram, and the line applies.
"""
import threading
import time

TTL_SECONDS = 900  # a decision older than this is not used (a turn queued that long is a new situation)
MAX_PENDING = 256

# Hermes's runner skips its own voice reply after any text_to_speech call in the turn, a failed one too. After a text
# message, or a voice message whose reply was streamed, that leaves the chat with no voice note; after a voice message
# with an unstreamed reply the adapter may still read the reply, without cues. Either way a failed call loses the cues or
# the voice note, so the line asks the agent to recover it once.
FACT = ("Hermes will read your reply to this message aloud, without cues. Say it yourself with text_to_speech, score "
        "included, and keep code and long lists to the written reply; Hermes then sends no voice note of its own. If "
        "that call fails, fix the cause, such as text that is too long, and try once more: a failed call can leave no "
        "voice note at all.")


def plays_in_voice_channel(event, gateway):
    """Whether Hermes would play this message's voice reply in a Discord voice channel: the test in
    ``_deliver_voice_reply``. Anything unknown counts as yes, so the message gets no line."""
    try:
        adapter = gateway._delivery_adapter_for(event.source)
        guild_id = gateway._get_guild_id(event)
        play = getattr(adapter, "play_in_voice_channel", None)
        is_in_vc = getattr(adapter, "is_in_voice_channel", None)
        return bool(guild_id and callable(play) and callable(is_in_vc) and is_in_vc(guild_id))
    except Exception:
        return True


class VoiceReplies:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._pending = {}
        self._lock = threading.Lock()
        self.check = None  # Hermes's voice-reply check at the last message: None (none seen), "ok", "missing", "error"

    def observe(self, event, gateway):
        """pre_gateway_dispatch: record whether Hermes will voice the reply to *event*. Never changes dispatch."""
        source = getattr(event, "source", None)
        platform = str(getattr(getattr(source, "platform", None), "value", "") or "")
        chat = str(getattr(source, "chat_id", "") or "")
        if not platform or not chat:
            return
        # The id the turn exports as HERMES_SESSION_MESSAGE_ID (gateway/run.py: source.message_id), never event.message_id.
        message = str(getattr(source, "message_id", "") or "")
        decide = getattr(gateway, "_should_send_voice_reply", None)
        voiced = False
        if not (platform == "discord" and plays_in_voice_channel(event, gateway)):
            if not callable(decide):
                self.check = "missing"
            else:
                try:
                    voiced = decide(event, "reply", [], already_sent=True) is True
                    self.check = "ok"
                except Exception:
                    voiced = False
                    self.check = "error"
        now = self.clock()
        with self._lock:
            # Keyed by message, so other messages in the chat (a group, a topic, a refused sender) never overwrite it.
            self._pending.pop((platform, chat, message), None)
            self._pending[(platform, chat, message)] = (voiced, now)
            while self._pending and (len(self._pending) > MAX_PENDING
                                     or now - next(iter(self._pending.values()))[1] > TTL_SECONDS):
                self._pending.pop(next(iter(self._pending)))

    def voiced(self, env):
        """For the turn in *env*'s session: True when Hermes said it will voice this message's reply. Each decision is
        used once: a queued or interrupting follow-up never passes the ingress hook and keeps the first message's id,
        so it must not inherit that message's answer."""
        key = (str(env("HERMES_SESSION_PLATFORM") or ""), str(env("HERMES_SESSION_CHAT_ID") or ""),
               str(env("HERMES_SESSION_MESSAGE_ID") or ""))
        with self._lock:
            entry = self._pending.pop(key, None)
        if entry is None:
            return False
        voiced, stamp = entry
        return voiced and self.clock() - stamp <= TTL_SECONDS


def voice_fact(settings, replies, env):
    """The line for this turn, only where the chip is the speech provider and supplies the guide."""
    if settings.get("guide_mode") != "generate" or settings.tts_provider() != "emotion-chip":
        return ""
    return FACT if replies.voiced(env) else ""


def voice_coverage(settings, replies):
    """One status value: whether the voice-replies line can reach this profile's chats, and if not, why."""
    provider = settings.tts_provider()
    if provider != "emotion-chip" or settings.get("guide_mode") != "generate":
        return "off (needs tts.provider: emotion-chip and guide_mode: generate)"
    if replies is None:
        return "off until Hermes restarts (the chip read other settings when it started)"
    return {None: "not checked yet (the chip has seen no message from a covered platform in this process)",
            "ok": "covered (Discord: not while the bot is in a voice channel)",
            "missing": "not covered (this Hermes has no voice-reply check)",
            "error": "not covered (Hermes's voice-reply check failed)"}[replies.check]
