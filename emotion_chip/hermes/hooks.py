"""Accept future host kwargs while counting only named task/transport facts."""
from .situation import situation_line

HOOKS = ("pre_llm_call", "post_tool_call", "api_request_error", "on_session_start", "on_session_end")


class Hooks:
    def __init__(self, settings, situation):
        self.settings = settings
        self.situation = situation

    def post_tool_call(self, **kwargs):
        status = kwargs.get("status")
        if status not in {"ok", "error"}:
            return

        def change(row):
            if status == "error":
                row["tool_error_streak"] += 1
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
        return {"context": line} if line else None
