"""Pure probe logic shared by the bench-probe plugin, the in-host turn driver and the runner.

Records hashes, lengths, booleans and token counts only: never prompt text, messages or headers.
"""
import hashlib
import json
import os
import re

CHIP_SECTION = "Voice delivery (Hermes Emotion Chip)"
SKILL_ROW = "emotion-chip-voice"
# Fixed here; the runner adds the bench machine's own repo paths and worktree names (required in its paths file)
# through the environment, since those cannot be committed.
FIXED_CANARIES = ("round-trip rule", "Lane Contract", "AGENTS.md")
NEGATIVE_CANARIES = FIXED_CANARIES + tuple(
    c for c in json.loads(os.environ.get("BENCH_PROBE_CANARIES") or "[]") if c not in FIXED_CANARIES)
# Hermes' per-session lines (timestamp line and its trailer); everything else must be byte-stable within an arm.
_VOLATILE = re.compile(r"^(Conversation started|Today's date \(as of|Timezone|Session ID):")


def text_of(value):
    """A system prompt as text: a string, or the text of a content-part list."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_of(part.get("text", "")) if isinstance(part, dict) else text_of(part) for part in value)
    if isinstance(value, dict):
        return text_of(value.get("text") or value.get("content") or "")
    return "" if value is None else str(value)


def normalise(prompt, item_dir=None):
    """The prompt minus its timestamp lines, with this item's own directory written as <ITEM>."""
    text = text_of(prompt)
    if item_dir:
        text = text.replace(str(item_dir), "<ITEM>")
    return "\n".join(line for line in text.split("\n") if not _VOLATILE.match(line))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canaries(*texts):
    """Which negative canaries occur anywhere in the given texts."""
    return {canary: any(canary in text for text in texts) for canary in NEGATIVE_CANARIES}


def tool_names(tools):
    names = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = function.get("name") or tool.get("name")
        if isinstance(name, str):
            names.append(name)
    return sorted(names)


def _dump(value):
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def request_row(kwargs, item_dir=None):
    """The pre_api_request record. A missing or empty system prompt is invalid evidence: no hash, flagged."""
    raw_system = text_of(kwargs.get("system_prompt"))
    system = normalise(raw_system, item_dir)
    body = (kwargs.get("request") or {}).get("body") or {}
    names = tool_names(body.get("tools"))
    valid = bool(raw_system.strip())
    return {"event": "pre_api_request", "api_call_count": kwargs.get("api_call_count"),
            "invalid": None if valid else "missing or empty system prompt",
            "system_sha256": digest(system) if valid else None, "system_chars": len(system),
            "chip_section": CHIP_SECTION in raw_system, "skill_row": SKILL_ROW in raw_system,
            "tools_sha256": digest("\n".join(names)), "tool_count": len(names),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "canaries": canaries(raw_system, _dump(kwargs.get("request_messages")), _dump(kwargs.get("request")))}


def response_row(kwargs):
    """The post_api_request record: upstream usage numbers when the provider reports them."""
    usage = kwargs.get("usage") if isinstance(kwargs.get("usage"), dict) else None
    numbers = {k: v for k, v in (usage or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {"event": "post_api_request", "api_call_count": kwargs.get("api_call_count"),
            "finish_reason": kwargs.get("finish_reason"), "usage": numbers or None,
            "tool_calls": kwargs.get("assistant_tool_call_count")}


def upstream_row(payload, item_dir=None):
    """The request body the relay sends upstream: its own system hash and canaries (never its text)."""
    raw = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    system = text_of(body.get("system")) if isinstance(body, dict) else ""
    if not isinstance(body, dict) or not system.strip():
        # Unreadable evidence is an error row (it fails assert 1), never a clean row.
        return {"event": "upstream_error", "error": "undecodable body" if not isinstance(body, dict)
                else "body without a system prompt", "body_chars": len(raw), "canaries": canaries(raw)}
    names = tool_names(body.get("tools"))
    return {"event": "upstream", "body_chars": len(raw), "system_sha256": digest(normalise(system, item_dir)),
            "system_chars": len(system), "chip_section": CHIP_SECTION in raw, "tool_count": len(names),
            "model": body.get("model"), "canaries": canaries(raw)}
