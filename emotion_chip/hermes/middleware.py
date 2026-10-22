"""Compatibility rendering before Hermes cleans tool text; fail open."""
from ..render import dialect_for, render
from .settings import sound_limit, session_env
from .situation import Situation


def make_middleware(settings, situation=None, env=None):
    situation = situation or Situation({})
    env = env or session_env
    def tool_request(**kwargs):
        try:
            args = kwargs.get("args") or {}
            if (kwargs.get("tool_name") != "text_to_speech" or settings.get("guide_mode") != "generate"
                    or "[" not in args.get("text", "")):
                return None
            provider = settings.tts_provider()
            if provider == "emotion-chip":
                return None
            dialect = "tagless"
            if provider == "fish-audio":
                dialect = dialect_for("fish", settings.fish_model(args.get("model")))
            elif provider == "elevenlabs" and settings.host("tts.elevenlabs.model_id", "").startswith(("eleven_v3", "eleven_v4")):
                dialect = "eleven_v4"
            text = render(args["text"], dialect, {"sounds_per_turn": sound_limit(settings, situation, env),
                                                "allow_speakers": False}).text
            return {"args": {**args, "text": text}}
        except Exception:
            return None
    return tool_request
