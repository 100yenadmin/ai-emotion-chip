"""Accept future host kwargs while counting only named task/transport facts."""
import json

from .settings import session_env, session_identity
from .situation import situation_line
from .voice import voice_fact

# How many recent tool_call_ids a session row keeps (a concurrent batch runs up to 8 tools at once).
COUNTED_CALLS = 16
HOOKS = ("pre_llm_call", "post_tool_call", "api_request_error", "on_session_start", "on_session_end", "transform_tool_result",
         "on_session_reset")


class Hooks:
    def __init__(self, settings, situation, env=None, *, start_turn=None, voice_home=None):
        self.settings = settings
        self.situation = situation
        self.env = env or session_env
        self.start_turn = start_turn
        self.voice_home = voice_home

    def _remember(self, kwargs):
        # A delegated subagent runs with its parent's session key still bound; mapping the key to the child
        # would point the parent chat's renders and /chip replies at the subagent.
        if kwargs.get("parent_session_id") or kwargs.get("platform") == "subagent":
            return
        session_id = kwargs.get("session_id")
        key = self.env("HERMES_SESSION_KEY")
        if key and session_id:
            with self.situation.lock:
                keys = self.situation.get("session_keys", {})
                keys[key] = session_id
                self.situation.set("session_keys", keys)

    def _count_error(self, row):
        row["tool_error_streak"] = row.get("tool_error_streak", 0) + 1
        row["successes_since_error"] = 0
        row["last_error_at"] = self.situation.clock()

    def post_tool_call(self, **kwargs):
        status = kwargs.get("status")
        if status not in {"ok", "error"}:
            return
        call_id = kwargs.get("tool_call_id") or ""

        def change(row):
            if status == "error":
                self._count_error(row)
            else:
                row["successes_since_error"] += 1
                row["tool_error_streak"] = 0
            if call_id:
                # transform_tool_result runs before this hook on Hermes's serial path and after it on the
                # concurrent one; the ids counted so far tell it which order it is in.
                row["counted_calls"] = (row.get("counted_calls", []) + [call_id])[-COUNTED_CALLS:]
        self.situation.update(kwargs.get("session_id") or "", change)

    def api_request_error(self, **kwargs):
        def change(row):
            row["transport_errors"] += 1
            row["retries"] = max(row["retries"], kwargs.get("retry_count") or 0)
        self.situation.update(kwargs.get("session_id") or "", change)

    def on_session_start(self, **kwargs):
        self.situation.update(kwargs.get("session_id") or "",
                              lambda row: row.update(started_at=self.situation.clock()))
        self._remember(kwargs)

    def on_session_end(self, **kwargs):
        if kwargs.get("interrupted"):
            self.situation.update(kwargs.get("session_id") or "",
                                  lambda row: row.update(interruptions=row["interruptions"] + 1))

    def on_session_reset(self, **kwargs):
        if kwargs.get("parent_session_id") or kwargs.get("platform") == "subagent":
            return
        old = kwargs.get("old_session_id")
        new = kwargs.get("new_session_id") or kwargs.get("session_id")
        if not old or not new or old == new:
            return
        with self.situation.lock:
            # Hermes passes no session key here, and several chats can share one session (/resume from a second
            # chat). Move only the chat that ran /new: the bound key, else (with no key bound) the one key on the
            # old session. When that is ambiguous, leave the map; each chat's next message maps its key again.
            keys = self.situation.get("session_keys", {})
            bound = self.env("HERMES_SESSION_KEY")
            on_old = [key for key, value in keys.items() if value == old]
            moved = bound if bound in on_old else (on_old[0] if not bound and len(on_old) == 1 else None)
            if moved is None:
                return
            # update() may evict the old row at the 50-row cap and prune its keys, so re-add the moved key after.
            self.situation.update(new, lambda row: None)
            self.situation.set("session_keys", {**self.situation.get("session_keys", {}), moved: new})

    def pre_llm_call(self, **kwargs):
        if self.start_turn and not (kwargs.get("parent_session_id") or kwargs.get("platform") == "subagent"):
            self.start_turn(kwargs.get("session_id") or session_identity(self.env, self.situation))
        mode = self.settings.get("signals")
        now = self.situation.clock()
        line = ""

        def change(row):
            nonlocal line
            line = situation_line(row, now, mode == "facts+affordance") if mode != "off" else ""
            if line:
                row["last_situation"] = line
        self.situation.update(kwargs.get("session_id") or "", change,
                              sender_id=kwargs.get("sender_id"), platform=kwargs.get("platform"))
        self._remember(kwargs)
        fact = ""
        if not (kwargs.get("parent_session_id") or kwargs.get("platform") == "subagent"):
            try:
                fact = voice_fact(self.settings, self.env, self.voice_home, kwargs.get("user_message"))
            except Exception:
                fact = ""  # an unreadable voice setting never costs the turn
        context = "\n".join(part for part in (line, fact) if part)
        return {"context": context} if context else None

    def transform_tool_result(self, **kwargs):
        mode = self.settings.get("signals")
        result = kwargs.get("result")
        if mode == "off" or kwargs.get("tool_name") == "text_to_speech" or not isinstance(result, str):
            return None
        session_id = kwargs.get("session_id") or ""
        row = self.situation.session(session_id)
        status = kwargs.get("status")
        call_id = kwargs.get("tool_call_id") or ""
        if status == "ok" or (status != "error" and row.get("tool_error_streak", 0) < 1):
            return None
        if status == "error" and call_id and call_id not in row.get("counted_calls", []):
            # Serial path: post_tool_call has not counted this failure yet, so count it in a copy for the line.
            row = dict(row)
            self._count_error(row)
        line = situation_line(row, self.situation.clock(), mode == "facts+affordance")
        if not line:
            return None
        try:
            value = json.loads(result)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, dict) and "situation" not in value:
            # Splice the key in so the tool's formatting, number spellings and key order survive.
            head = result.rstrip()[:-1].rstrip()
            transformed = head + (", " if value else "") + '"situation": ' + json.dumps(line, ensure_ascii=False) + "}"
        else:
            transformed = result + "\n\nSituation: " + line
        self.situation.update(session_id, lambda row: row.update(last_situation=line))
        return transformed
