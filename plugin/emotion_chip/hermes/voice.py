"""Whether Hermes reads this chat's replies aloud, by the gateway's own rule (gateway/run_voice.py).

Hermes speaks a reply itself (auto voice reply) when the chat's /voice mode says so, kept in
gateway_voice_mode.json under ``[<profile>:]<platform>:<chat_id>``: "all" voices every reply, "voice_only" only
replies to voice messages, "off" none. A chat with no mode falls back to ``voice.auto_tts``, which voices replies
to voice messages only. Those replies reach the voice without cues, so the agent is told, on the turn, to say the
answer itself with text_to_speech; Hermes then sends no voice note of its own (it skips its auto voice reply when the
agent called text_to_speech this turn, and when the reply already carries audio).
"""
import json
import sys
from pathlib import Path

VOICE_MODE_FILE = "gateway_voice_mode.json"
# Sessions with no chat that can get a voice note: the CLI and Desktop speak through their own clients.
NO_VOICE_NOTES = {"", "cli", "local", "desktop", "a2a", "subagent", "cron", "kanban", "api_server", "webhook"}

SAY_IT = ("Say it yourself with text_to_speech, score included, and keep code and long lists to the written "
          "reply; Hermes then sends no voice note of its own.")
FACTS = {
    "all": "Hermes reads your reply in this chat aloud, without cues. " + SAY_IT,
    "voice": "This is a voice message, and Hermes reads your reply to it aloud, without cues. " + SAY_IT,
}
# How Hermes hands the agent a voice message (gateway/run_inbound_transcription.py): the transcript as a quoted
# line, or a note when there is no transcript; a caption follows after a blank line.
VOICE_NOTES = ("[The user sent a voice message", "[voice message could not be transcribed")


def _mode_file(home=None):
    if home is not None:
        return Path(home) / VOICE_MODE_FILE
    runner = getattr(sys.modules.get("gateway.run"), "GatewayRunner", None)
    path = getattr(runner, "_VOICE_MODE_PATH", None)
    if path is not None:
        return Path(path)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / VOICE_MODE_FILE


def _chat_mode(modes, platform, chat, profile):
    keys = [f"{profile}:{platform}:{chat}"] if profile and profile != "default" else []
    keys.append(f"{platform}:{chat}")
    for key in keys:
        if key in modes:
            return modes[key]
    # The gateway keys by the profile that owns the bot, which a session may name differently; take a lone match.
    suffix = f":{platform}:{chat}"
    matches = [mode for key, mode in modes.items() if key.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def voice_replies(settings, env, home=None):
    """ "all", "voice" (replies to voice messages) or None for this session's chat; None when unknown."""
    platform = str(env("HERMES_SESSION_PLATFORM") or "").strip().lower()
    chat = str(env("HERMES_SESSION_CHAT_ID") or "").strip()
    if not chat or platform in NO_VOICE_NOTES:
        return None
    try:
        modes = json.loads(_mode_file(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, ImportError):
        modes = {}
    mode = _chat_mode(modes, platform, chat, str(env("HERMES_SESSION_PROFILE") or "").strip()) \
        if isinstance(modes, dict) else None
    if mode == "all":
        return "all"
    if mode == "voice_only":
        return "voice"
    if mode == "off":
        return None
    return "voice" if settings.host("voice.auto_tts") is True else None


def looks_like_voice(message):
    """Whether the user's message has the shape Hermes gives a voice message."""
    if isinstance(message, list):
        message = "\n\n".join(part.get("text") or "" for part in message
                               if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(message, str):
        return False
    first = message.strip().split("\n\n", 1)[0].strip()
    return (len(first) >= 2 and first[0] == first[-1] == '"') or first.startswith(VOICE_NOTES)


def voice_fact(settings, env, home=None, message=None):
    """The line for the agent when Hermes will read this reply aloud; only where the chip is the speech provider and
    supplies the guide. With voice replies for voice messages only, just on a message shaped like a voice message."""
    if settings.get("guide_mode") != "generate" or settings.tts_provider() != "emotion-chip":
        return ""
    mode = voice_replies(settings, env, home)
    if mode == "voice" and not looks_like_voice(message):
        return ""
    return FACTS.get(mode, "")
