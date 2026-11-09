"""Tell the agent when Hermes will read its reply aloud, so it can say the reply itself with its cues.

With voice replies on in a chat (/voice, or voice.auto_tts), Hermes speaks the reply text itself, without cues. When
the agent calls text_to_speech in the turn instead, Hermes sends no voice note of its own: the runner skips its voice
reply after a text_to_speech call (gateway/run_voice.py ``_should_send_voice_reply``), and the adapter's auto voice
reply needs a reply without media, which the tool's audio gives it (``_append_auto_media_tags``).

Whether Hermes will voice a reply is Hermes's own decision, asked at ingress in ``pre_gateway_dispatch``, which gets
the inbound event and, in process, the gateway runner: ``_should_send_voice_reply(event, …, already_sent=True)``
answers it for the chat's /voice mode (keyed by the bot that owns the chat), ``voice.auto_tts`` and the message's
type. The answer is kept in memory under the chat and message id, and ``pre_llm_call`` turns it into one line for that
message's turn. Anything unknown (an isolated plugin host, a missing method, an error) means no line, which is what
Hermes does without the chip.
"""
import threading
import time

# Discord voice channels play Hermes's own voice reply in the channel; a clip from the agent would go to the text
# channel instead, so Discord gets no line.
NOT_COVERED = {"discord"}
TTL_SECONDS = 900  # a decision older than this is not used (a turn queued that long is a new situation)
MAX_PENDING = 256

FACT = ("Hermes will read your reply to this message aloud, without cues. Say it yourself with text_to_speech, score "
        "included, and keep code and long lists to the written reply; Hermes then sends no voice note of its own.")


class VoiceReplies:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._pending = {}
        self._lock = threading.Lock()

    def observe(self, event, gateway):
        """pre_gateway_dispatch: record whether Hermes will voice the reply to *event*. Never changes dispatch."""
        source = getattr(event, "source", None)
        platform = str(getattr(getattr(source, "platform", None), "value", "") or "")
        chat = str(getattr(source, "chat_id", "") or "")
        if not platform or not chat:
            return
        message = str(getattr(source, "message_id", "") or getattr(event, "message_id", "") or "")
        decide = getattr(gateway, "_should_send_voice_reply", None)
        voiced = False
        if platform not in NOT_COVERED and callable(decide):
            try:
                voiced = decide(event, "reply", [], already_sent=True) is True
            except Exception:
                voiced = False
        with self._lock:
            self._pending.pop((platform, chat), None)
            self._pending[(platform, chat)] = (message, voiced, self.clock())
            while len(self._pending) > MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))

    def voiced(self, env):
        """For the turn in *env*'s session: True when Hermes said it will voice this message's reply."""
        key = (str(env("HERMES_SESSION_PLATFORM") or ""), str(env("HERMES_SESSION_CHAT_ID") or ""))
        message = str(env("HERMES_SESSION_MESSAGE_ID") or "")
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                return False
            recorded, voiced, stamp = entry
            if self.clock() - stamp > TTL_SECONDS or (message or recorded) and message != recorded:
                return False
            if not message:
                self._pending.pop(key, None)  # no id to match: use the decision once
            return voiced


def voice_fact(settings, replies, env):
    """The line for this turn, only where the chip is the speech provider and supplies the guide."""
    if settings.get("guide_mode") != "generate" or settings.tts_provider() != "emotion-chip":
        return ""
    return FACT if replies.voiced(env) else ""
