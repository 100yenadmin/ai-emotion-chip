"""Compatibility rendering before Hermes cleans tool text; fail open."""
from ..data import load_data
from ..render import dialect_for
from . import provider as provider_module
from .fishtags import adapt_all, fish_adapter, settled_render
from .settings import sound_limit, session_env
from .situation import Situation


def make_middleware(settings, situation=None, env=None):
    situation = situation or Situation({})
    env = env or session_env

    def tool_request(**kwargs):
        try:
            args = kwargs.get("args") or {}
            if kwargs.get("tool_name") != "text_to_speech" or settings.get("guide_mode") != "generate":
                return None
            provider = settings.tts_provider()
            if provider == "emotion-chip":
                return None
            text = args.get("text", "")
            dialect, adapt = "tagless", None
            if provider == "fish-audio":
                # Fish gets tts.model from Hermes, never the chip's own fish_model setting.
                dialect = dialect_for("fish", settings.resolve_fish_model(settings.host("tts.model"), use_setting=False).effective)
                try:  # every Fish model: see the provider
                    adapt = fish_adapter(provider_module._fish())
                    text = adapt_all(text, adapt)
                except Exception:
                    adapt = None
            elif provider == "elevenlabs" and settings.host("tts.elevenlabs.model_id", "").startswith(("eleven_v3", "eleven_v4")):
                dialect = "eleven_v4"
            if "[" not in text:
                return None
            options = {"sounds_per_turn": sound_limit(settings, situation, env), "allow_speakers": False}
            rendered, _ = settled_render(text, dialect, options, load_data(), adapt)
            return {"args": {**args, "text": rendered.text}}
        except Exception:
            return None
    return tool_request
