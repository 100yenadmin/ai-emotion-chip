"""Plain-text /chip controls and authored-delivery explanations."""
from copy import deepcopy

import os

from ..render import dialect_for
from . import provider as provider_module
from .provider import last_render, last_render_lock
from .settings import session_identity

USAGE = "Usage: /chip [status|why|quiet [on|off]|register [clear]]"


def _has_secret(name):
    """True or False for the active profile's secret *name*; None when this context cannot tell (a multiplexed gateway
    with no profile scope bound), so status never reports a key missing that a turn would find."""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return bool(os.environ.get(name, "").strip())
    try:
        return bool((get_secret(name) or "").strip())
    except UnscopedSecretError:
        return None


def setup_problems(settings):
    """What stops the chip from speaking with this profile's settings, each with its fix."""
    provider = settings.tts_provider()
    if provider != "emotion-chip":
        # Another voice speaks; the chip only adapts cues for it, so its own backend does not matter here.
        return [f"the speech provider is {provider}; set tts.provider: emotion-chip"]
    if settings.command_provider("emotion-chip"):
        return ["a command provider named emotion-chip (tts.providers or tts.emotion-chip) hides the plugin; rename it"]
    problems = []
    if settings.get("backend") == "eleven":
        if _has_secret("ELEVENLABS_API_KEY") is False:
            problems.append("add ELEVENLABS_API_KEY to the profile's .env")
        if not settings.eleven_voice(settings.call_voice()):
            problems.append("set the plugin's eleven_voice_id")
        return problems
    fish = provider_module._fish()
    if fish is None:
        return ["install and enable the fish-audio plugin, or set backend: eleven"]
    if _has_secret("FISH_API_KEY") is None:
        return problems  # Fish reads its key the same way; this context cannot tell
    try:
        available = fish.is_available()
    except Exception:
        return problems
    if not available:
        problems.append("the fish-audio plugin is not ready; check its FISH_API_KEY")
    return problems


def make_handler(settings, situation, env):
    def handle(raw_args):
        args = (raw_args or "status").strip().split()
        if not args:
            args = ["status"]
        session_id = session_identity(env, situation)
        if args == ["status"]:
            problems = setup_problems(settings)
            values = {"ready": "yes" if not problems else "no (" + "; ".join(problems) + ")"}
            values.update({key: settings.get(key) for key in ("backend", "fish_model", "guide_mode", "signals")})
            backend = settings.get("backend")
            model = settings.model(settings.host("tts.model"))
            if backend == "eleven":
                values.pop("fish_model")
                values.update(eleven_model=model, eleven_voice_id=settings.eleven_voice(settings.call_voice()))
            else:
                values["fish_model"] = settings.resolve_fish_model(settings.host("tts.model")).label
            values.update(session_id=session_id, dialect=str(dialect_for(backend, model)),
                          quiet="on" if situation.get("quiet", False) else "off",
                          delivery_log="on" if settings.get("delivery_log") else "off")
            return "; ".join(f"{key}: {value}" for key, value in values.items())
        if args == ["why"]:
            with last_render_lock:
                row = deepcopy(last_render.get(session_id, {}))
            if row:
                dropped = "; ".join(f"{d['kind']} {d['value']}: {d['reason']}" for d in row["dropped"]) or "none"
                backend = "ElevenLabs" if row.get("backend") == "eleven" else "Fish"
                text = (f"Dialect: {row['dialect']}; {backend} model: {row['fish_model']}; authored cue words: {', '.join(row['cue_words']) or 'none'}; "
                        f"dropped: {dropped}; "
                        f"params: {row['params']}")
                if row["notes"]:
                    text += "; notes: " + "; ".join(row["notes"])
            else:
                text = "No render recorded for this session."
            line = situation.session(session_id).get("last_situation", "")
            return text + ("\n" + line if line else "")
        if args in (["quiet"], ["quiet", "on"], ["quiet", "off"]):
            quiet = not situation.get("quiet", False) if len(args) == 1 else args[1] == "on"
            situation.set("quiet", quiet)
            return "Sounds quiet: " + ("on" if quiet else "off") + " (every chat on this profile)"
        if args == ["register", "clear"]:
            situation.update(session_id, lambda row: row.update(register=""))
            return "Register cleared."
        if args == ["register"]:
            return situation.session(session_id).get("register", "") or "No authored register recorded."
        return USAGE
    return handle
