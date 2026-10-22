"""Session task facts only; never infer user affect or agent cues."""
from copy import deepcopy
from datetime import datetime
from threading import RLock
import time

COUNTERS = ("tool_error_streak", "successes_since_error", "transport_errors", "retries", "interruptions")


class Situation:
    def __init__(self, state, clock=None):
        self.state = state
        self.clock = clock or time.time
        self.lock = RLock()

    def get(self, key, default=None):
        with self.lock:
            return deepcopy(self.state.get(key, default))

    def set(self, key, value):
        with self.lock:
            if hasattr(self.state, "set"):
                self.state.set(key, value)
            else:
                self.state[key] = value

    def session(self, session_id):
        return self.get("sessions", {}).get(session_id, {})

    def update(self, session_id, change, **metadata):
        with self.lock:
            sessions = self.get("sessions", {})
            row = sessions.pop(session_id, {key: 0 for key in COUNTERS})
            row.update({k: v for k, v in metadata.items() if v is not None})
            change(row)
            row["last_seen"] = self.clock()
            sessions[session_id] = row
            sessions = dict(sorted(sessions.items(), key=lambda item: item[1]["last_seen"])[-50:])
            self.set("sessions", sessions)
            return deepcopy(row)


def situation_line(row, now, affordance=False):
    if not any(row.get(k, 0) for k in COUNTERS):
        return ""
    facts = []
    for key, label in (("tool_error_streak", "tool errors in a row"),
                       ("successes_since_error", "tool successes since error"),
                       ("transport_errors", "transport errors"), ("retries", "API retries"),
                       ("interruptions", "interruptions")):
        if row.get(key, 0):
            facts.append(f"{row[key]} {label}")
    if "started_at" in row:
        minutes = max(0, int((now - row["started_at"]) / 60))
        facts.append(f"session {minutes // 60} h {minutes % 60} min")
    prefix = f"Situation at {datetime.fromtimestamp(now):%H:%M}: "
    suffix = " Stopping and reporting is fine." if affordance else ""
    # Keep whole facts and a complete affordance sentence within the host budget.
    while len(prefix + "; ".join(facts) + "." + suffix) > 200:
        facts.pop()
    return prefix + "; ".join(facts) + "." + suffix
