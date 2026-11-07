"""Plain-text /chip controls and authored-delivery explanations."""
from copy import deepcopy

from ..render import dialect_for
from .provider import last_render, last_render_lock
from .settings import session_identity

USAGE = "Usage: /chip [status|why|quiet [on|off]|register [clear]]"


def make_handler(settings, situation, env):
    def handle(raw_args):
        args = (raw_args or "status").strip().split()
        if not args:
            args = ["status"]
        session_id = session_identity(env, situation)
        if args == ["status"]:
            values = {key: settings.get(key) for key in ("backend", "fish_model", "guide_mode", "signals")}
            backend = settings.get("backend")
            model = settings.model(settings.host("tts.model"))
            if backend == "eleven":
                values.pop("fish_model")
                values.update(eleven_model=model, eleven_voice_id=settings.eleven_voice(settings.call_voice()))
            else:
                values["fish_model"] = model
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
            return "Sounds quiet: " + ("on" if quiet else "off")
        if args == ["register", "clear"]:
            situation.update(session_id, lambda row: row.update(register=""))
            return "Register cleared."
        if args == ["register"]:
            return situation.session(session_id).get("register", "") or "No authored register recorded."
        return USAGE
    return handle
