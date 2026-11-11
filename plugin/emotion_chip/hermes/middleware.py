"""Compatibility rendering before Hermes cleans tool text; fail open."""
from dataclasses import dataclass

from ..data import load_data
from ..model import Cue
from ..render import dialect_for
from ..score import header_cue, parse
from . import provider as provider_module
from .fishtags import adapt_all, fish_adapter, settled_render
from .settings import session_env, sounds_on
from .situation import Situation


@dataclass(frozen=True)
class CompatTarget:
    provider: str
    dialect: str
    adapt_fish: bool


def compat_target(settings):
    provider = settings.tts_provider()
    # A command provider is the user's own script (eva's sag-eva speaks ElevenLabs tags): leave its text alone.
    if provider == "emotion-chip" or settings.command_provider(provider):
        return None
    dialect = "tagless"
    if provider == "fish-audio":
        # Fish gets tts.model from Hermes, never the chip's own fish_model setting.
        dialect = dialect_for("fish", settings.resolve_fish_model(settings.host("tts.model"), use_setting=False).effective)
    elif provider == "elevenlabs":
        # A missing model_id selects Hermes's default (eleven_multilingual_v2), which reads bracket cues aloud; a null
        # or non-string one gets the same conservative tagless fallback.
        model_id = settings.host("tts.elevenlabs.model_id", "")
        if isinstance(model_id, str) and model_id.startswith(("eleven_v3", "eleven_v4")):
            dialect = "eleven_v4"
    # Keep the Dialect object (a str): its routing notes ("fish s1 takes fixed cues only") reach the preview.
    return CompatTarget(provider, dialect, provider == "fish-audio")


def taught_dialect(settings):
    target = compat_target(settings)
    return target.dialect if target is not None else dialect_for(
        settings.get("backend"), settings.model(settings.host("tts.model")))


def compat_render(settings, situation, text, target, *, with_cue_words=False):
    """Return passed-on text, optional render, and words; explanation parsing is preview-only."""
    original, adapt = text, None
    try:
        if target.adapt_fish:
            try:  # every Fish model: see the provider
                adapt = fish_adapter(provider_module._fish())
                text = adapt_all(text, adapt)
            except Exception:
                adapt = None
        if "[" not in text:
            return original, None, []
        options = {"sounds": sounds_on(situation), "allow_speakers": False}
        data = load_data()
        rendered, added = settled_render(text, target.dialect, options, data, adapt)
    except Exception:
        return original, None, []
    words = []
    if with_cue_words:
        score = parse(text, data)
        opener = header_cue(score.header)
        words = (list(opener.words) if opener else []) + [
            word for sentence in score.sentences for token in sentence.tokens if isinstance(token, Cue)
            for word in token.words] + added
    return rendered.text, rendered, words


def make_middleware(settings, situation=None, env=None):
    situation = situation or Situation({})
    env = env or session_env

    def tool_request(**kwargs):
        try:
            args = kwargs.get("args") or {}
            if kwargs.get("tool_name") != "text_to_speech" or settings.get("guide_mode") != "generate":
                return None
            target = compat_target(settings)
            if target is None:
                return None
            text, rendered, _ = compat_render(settings, situation, args.get("text", ""), target)
            if rendered is None:
                return None
            return {"args": {**args, "text": text}}
        except Exception:
            return None
    return tool_request
