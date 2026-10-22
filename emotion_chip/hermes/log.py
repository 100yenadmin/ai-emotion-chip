"""Append delivery metadata, never the spoken text, with bounded rotation."""
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock

from .settings import plugin_data_dir

MAX_BYTES = 5 * 1024 * 1024
_lock = Lock()


class DeliveryLog:
    def __init__(self, settings, data_dir=None):
        self.settings = settings
        self.data_dir = data_dir or plugin_data_dir

    def append(self, session_id, metadata, rendered, cue_words, chars, now):
        if not self.settings.get("delivery_log"):
            return
        row = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(),
               "session_id": session_id, "sender_id": metadata.get("sender_id", ""),
               "platform": metadata.get("platform", ""), "dialect": rendered.dialect,
               "cue_words": cue_words, "group": rendered.group, "params": rendered.params,
               "dropped_kinds": [d.kind for d in rendered.dropped], "chars": chars}
        encoded = json.dumps(row, ensure_ascii=False) + "\n"
        with _lock:
            path = Path(self.data_dir()) / "delivery.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(encoded.encode("utf-8")) >= MAX_BYTES:
                path.replace(path.with_name("delivery.1.jsonl"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
