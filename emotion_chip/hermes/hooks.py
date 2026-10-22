"""Accept future host kwargs while counting only named task/transport facts."""
import json

from .settings import session_env, session_identity
from .situation import situation_line

HOOKS = ("pre_llm_call", "post_tool_call", "api_request_error", "on_session_start", "on_session_end", "transform_tool_result")


class Hooks:
    def __init__(self, settings, situation, env=None):
        self.settings = settings
        self.situation = situation
        self.env = env or session_env

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

    def post_tool_call(self, **kwargs):
        status = kwargs.get("status")
        if status not in {"ok", "error"}:
            return

        def change(row):
            if status == "error":
                row["tool_error_streak"] += 1
                row["successes_since_error"] = 0
                row["last_error_at"] = self.situation.clock()
            else:
                row["successes_since_error"] += 1
                row["tool_error_streak"] = 0
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

    def pre_llm_call(self, **kwargs):
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
        return {"context": line} if line else None

    def transform_tool_result(self, **kwargs):
        mode = self.settings.get("signals")
        session_id = kwargs.get("session_id") or session_identity(self.env, self.situation)
        row = self.situation.session(session_id)
        if mode == "off" or row.get("tool_error_streak", 0) < 1 or kwargs.get("tool_name") == "text_to_speech":
            return None
        result = kwargs.get("result")
        if not isinstance(result, str):
            return None
        line = situation_line(row, self.situation.clock(), mode == "facts+affordance")
        try:
            value = json.loads(result)
        except ValueError:
            value = None
        if isinstance(value, dict):
            value["situation"] = line
            transformed = json.dumps(value, ensure_ascii=False)
        else:
            transformed = result + "\n\nSituation: " + line
        self.situation.update(session_id, lambda row: row.update(last_situation=line))
        return transformed
