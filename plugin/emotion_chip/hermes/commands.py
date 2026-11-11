"""Plain-text /chip controls and authored-delivery explanations."""
from copy import deepcopy

import os

from ..render import dialect_for
from . import provider as provider_module
from .middleware import compat_target
from .provider import last_render, last_render_lock
from .settings import session_identity
from .voice import voice_coverage

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


def readiness(settings):
    """(problems, unchecked): what stops the chip from speaking with this profile's settings, each with its fix, and
    what this context could not check, so status never calls an unchecked setup ready."""
    provider = settings.tts_provider()
    if provider != "emotion-chip":
        # Another voice speaks; the chip only adapts cues for it, so its own backend does not matter here.
        return [f"the speech provider is {provider}; set tts.provider: emotion-chip"], []
    problems, unchecked = [], []
    backend_problem = settings.backend_problem()
    if backend_problem:
        problems.append(backend_problem)
    if settings.command_provider("emotion-chip"):
        return problems + ["a command provider named emotion-chip (tts.providers or tts.emotion-chip) hides the plugin; rename it"], []
    if settings.get("backend") == "eleven":
        key = _has_secret("ELEVENLABS_API_KEY")
        if key is False:
            problems.append("add ELEVENLABS_API_KEY to the profile's .env")
        elif key is None:
            unchecked.append("the ElevenLabs key")
        if not settings.eleven_voice(settings.call_voice()):
            problems.append("set the plugin's eleven_voice_id")
        return problems, unchecked
    fish = provider_module._fish()
    if fish is None:
        return problems + ["install and enable the fish-audio plugin, or set backend: eleven"], []
    if _has_secret("FISH_API_KEY") is None:
        return problems, ["the Fish Audio key"]  # Fish reads its key the same way; this context cannot tell
    try:
        available = fish.is_available()
    except Exception:
        return problems, ["the fish-audio plugin, whose readiness check raised an error"]
    if not available:
        problems.append("the fish-audio plugin is not ready; check its FISH_API_KEY")
    return problems, unchecked


def setup_problems(settings):
    """What stops the chip from speaking with this profile's settings, each with its fix."""
    return readiness(settings)[0]


def ready_value(settings, facts=None):
    problems, unchecked = readiness(settings) if facts is None else facts
    if problems:
        return "no (" + "; ".join(problems) + ")"
    if unchecked:
        return "probably (not checked from here: " + ", ".join(unchecked) + ")"
    return "yes"


def build_status(settings, situation, env, voice_replies=None):
    """Shared status facts for the human command and the read-only agent tool."""
    problems, unchecked = readiness(settings)
    backend = settings.get("backend")
    model = settings.model(settings.host("tts.model"))
    target = compat_target(settings)
    session_id = session_identity(env, situation)
    values = dict(ready=ready_value(settings, (problems, unchecked)).split(" ", 1)[0],
                  problems=problems, not_checked=unchecked, backend=backend, model=model,
                  dialect=target.dialect if target is not None else str(dialect_for(backend, model)),
                  guide_mode=settings.get("guide_mode"),
                  signals=settings.get("signals"), quiet=bool(situation.get("quiet", False)),
                  delivery_log=bool(settings.get("delivery_log")),
                  voice_replies=voice_coverage(settings, voice_replies), session_id=session_id,
                  register=situation.session(session_id).get("register", ""))
    if target is not None:
        if target.provider == "fish-audio":
            values["fish_model"] = settings.resolve_fish_model(settings.host("tts.model"), use_setting=False).label
    elif backend == "eleven":
        values["eleven_voice_id"] = settings.eleven_voice(settings.call_voice())
    else:
        values["fish_model"] = settings.resolve_fish_model(settings.host("tts.model")).label
    return values


def make_handler(settings, situation, env, voice_replies=None):
    def handle(raw_args):
        args = (raw_args or "status").strip().split()
        if not args:
            args = ["status"]
        session_id = session_identity(env, situation)
        if args == ["status"]:
            facts = build_status(settings, situation, env, voice_replies)
            values = {"ready": ready_value(settings, (facts["problems"], facts["not_checked"]))}
            values.update({key: facts[key] for key in ("backend", "fish_model", "guide_mode", "signals") if key in facts})
            if facts["backend"] == "eleven":
                values["eleven_model"] = facts["model"]
                if "eleven_voice_id" in facts:
                    values["eleven_voice_id"] = facts["eleven_voice_id"]
            values.update(session_id=facts["session_id"], dialect=facts["dialect"],
                          quiet="on" if facts["quiet"] else "off",
                          delivery_log="on" if facts["delivery_log"] else "off", voice_replies=facts["voice_replies"])
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
