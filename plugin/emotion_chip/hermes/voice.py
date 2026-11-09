"""Whether Hermes reads this chat's replies aloud, by the gateway's own rule (gateway/run_voice.py).

Hermes speaks a reply itself (auto voice reply) when the chat's /voice mode says so, kept in
gateway_voice_mode.json under ``[<profile>:]<platform>:<chat_id>``: "all" voices every reply, "voice_only" only
replies to voice messages, "off" none. A chat with no mode falls back to ``voice.auto_tts``, which voices replies
to voice messages only. Those replies reach the voice without cues, so the agent is told; it can say the answer
itself with text_to_speech, and Hermes then sends no voice note of its own.
"""
import json
import sys
from pathlib import Path

VOICE_MODE_FILE = "gateway_voice_mode.json"
# Sessions with no chat that can get a voice note: the CLI and Desktop speak through their own clients.
NO_VOICE_NOTES = {"", "cli", "local", "desktop", "a2a", "subagent", "cron", "kanban", "api_server", "webhook"}

FACTS = {
    "all": "Voice replies are on in this chat: Hermes reads every reply aloud.",
    "voice": "Voice replies are on in this chat for voice messages: Hermes reads a reply to a voice message aloud. "
             "A voice message arrives as a quoted transcript.",
}


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


def voice_fact(settings, env, home=None):
    """The fact line for the agent, only where the chip's guide is in the prompt (the guide says what to do)."""
    if settings.get("guide_mode") != "generate" or settings.tts_provider() != "emotion-chip":
        return ""
    return FACTS.get(voice_replies(settings, env, home), "")
