"""Compatibility rendering before Hermes cleans tool text; fail open."""
from ..render import render


def make_middleware(settings):
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
                dialect = "fish_s2"
            elif provider == "elevenlabs" and settings.host("tts.elevenlabs.model_id", "").startswith(("eleven_v3", "eleven_v4")):
                dialect = "eleven_v4"
            text = render(args["text"], dialect, {"sounds_per_turn": settings.get("sounds_per_turn"),
                                                "allow_speakers": False}).text
            return {"args": {**args, "text": text}}
        except Exception:
            return None
    return tool_request
