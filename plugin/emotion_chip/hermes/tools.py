"""Read-only agent views of profile readiness and offline score rendering."""
import json

from ..cli import DIALECTS
from ..model import Cue
from ..render import dialect_for, render
from ..score import header_cue, parse
from .commands import build_status
from .settings import sounds_on

DESCRIPTIONS = {
    "chip_status": "Emotion Chip: whether your voice can speak with this profile's settings, what to fix if not, "
                   "and the engine, dialect and voice in use. Read-only.",
    "chip_preview": "Emotion Chip: show what the voice engine would receive for a delivery score, and which cues "
                    "it would drop and why, without synthesizing. Offline, no cost.",
}


def register_tools(ctx, settings, situation, env, voice_replies=None):
    def available():
        return settings.get("guide_mode") == "generate"

    # Newer Hermes caches probes, with a grace period for failures; config checks must be immediate.
    try:
        from tools.registry import no_cache_check_fn
    except ImportError:
        pass
    else:
        no_cache_check_fn(available)

    def status(args):
        return json.dumps(build_status(settings, situation, env, voice_replies))

    def preview(args):
        if not isinstance(args, dict) or not isinstance(args.get("score"), str):
            return json.dumps({"error": "score must be a string"})
        text = args["score"]
        if len(text) > 10000:
            return json.dumps({"error": "score exceeds 10000 characters"})
        dialect = args.get("dialect", str(dialect_for(
            settings.get("backend"), settings.model(settings.host("tts.model")))))
        if dialect not in DIALECTS:
            return json.dumps({"error": "unknown dialect"})
        score = parse(text)
        result = render(score, dialect, {"sounds": sounds_on(situation), "allow_speakers": False})
        opener = header_cue(score.header)
        words = (list(opener.words) if opener else []) + [
            word for sentence in score.sentences for token in sentence.tokens if isinstance(token, Cue)
            for word in token.words]
        return json.dumps(dict(dialect=result.dialect, rendered=result.text, cue_words=words,
                               dropped=[dict(kind=d.kind, raw=d.value, reason=d.reason) for d in result.dropped],
                               params=result.params, notes=result.notes, chars=len(result.text)))

    properties = {"score": {"type": "string", "maxLength": 10000},
                  "dialect": {"type": "string", "enum": list(DIALECTS)}}
    for name, handler in (("chip_status", status), ("chip_preview", preview)):
        schema = dict(name=name, description=DESCRIPTIONS[name], parameters={
            "type": "object", "properties": properties if name == "chip_preview" else {},
            "required": ["score"] if name == "chip_preview" else []})
        ctx.register_tool(name=name, toolset="emotion-chip", schema=schema, handler=handler,
                          check_fn=available, description=DESCRIPTIONS[name])
