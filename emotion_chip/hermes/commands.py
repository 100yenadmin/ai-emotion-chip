"""Plain-text /chip controls and authored-delivery explanations."""
from copy import deepcopy

from ..render import dialect_for
from .provider import last_render, last_render_lock

USAGE = "Usage: /chip [status|why|quiet [on|off]|register [clear]]"


def make_handler(settings, situation, env):
    def handle(raw_args):
        args = (raw_args or "status").strip().split()
        if not args:
            args = ["status"]
        if args == ["status"]:
            values = {key: settings.get(key) for key in ("backend", "fish_model", "guide_mode", "signals", "sounds_per_turn")}
            values.update(dialect=str(dialect_for("fish", settings.fish_model())),
                          quiet="on" if situation.get("quiet", False) else "off",
                          delivery_log="on" if settings.get("delivery_log") else "off")
            return "; ".join(f"{key}: {value}" for key, value in values.items())
        if args == ["why"]:
            with last_render_lock:
                row = deepcopy(last_render)
            if row.get("session_id") != env("HERMES_SESSION_ID"):
                row = {}
            if row:
                dropped = "; ".join(f"{d['kind']} {d['value']}: {d['reason']}" for d in row["dropped"]) or "none"
                caps = [d["reason"] for d in row["dropped"] if d["reason"] in {"sounds_per_turn", "breath density"}]
                text = (f"Dialect: {row['dialect']}; authored cue words: {', '.join(row['cue_words']) or 'none'}; "
                        f"dropped: {dropped}; caps applied: {', '.join(caps) or 'none'}; "
                        f"params: {row['params']}")
                if row["notes"]:
                    text += "; notes: " + "; ".join(row["notes"])
            else:
                text = "No render recorded for this session."
            line = situation.session(env("HERMES_SESSION_ID")).get("last_situation", "")
            return text + ("\n" + line if line else "")
        if args in (["quiet"], ["quiet", "on"], ["quiet", "off"]):
            quiet = not situation.get("quiet", False) if len(args) == 1 else args[1] == "on"
            situation.set("quiet", quiet)
            return "Sounds quiet: " + ("on" if quiet else "off")
        if args == ["register", "clear"]:
            situation.set("register", "")
            return "Register cleared."
        if args == ["register"]:
            return situation.get("register", "") or "No authored register recorded."
        return USAGE
    return handle
