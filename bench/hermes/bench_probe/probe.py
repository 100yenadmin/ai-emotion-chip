"""Pure probe logic shared by the bench-probe plugin, the in-host turn driver and the runner.

Records hashes, lengths, booleans and token counts only: never prompt text, messages or headers.
"""
import hashlib
import copy
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
_VOLATILE = re.compile(r"^(Conversation started|Today's date \(as of|Timezone|Session ID:)")


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
    # E7 alone opts in: remove exact frozen persona bytes, never arbitrary canary words.
    from pathlib import Path
    exclusions = json.loads(os.environ.get("BENCH_PROBE_EXCLUDE_FILES") or "[]")
    for path in exclusions:
        snapshot = Path(path).read_text(encoding="utf-8")
        if snapshot:
            for literal in (snapshot, json.dumps(snapshot)[1:-1], json.dumps(snapshot, ensure_ascii=False)[1:-1]):
                texts = tuple(text.replace(literal, "") for text in texts)
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


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def component(value):
    text = value if isinstance(value, str) else canonical(value)
    return {"sha256": digest(text), "chars": len(text)}


def content_only(value, tool=False):
    if isinstance(value, list):
        return [content_only(v, tool) for v in value]
    if isinstance(value, dict):
        return {k: content_only(v, k == "tools") for k, v in value.items()
                if k != "cache_control" and not (tool and k == "eager_input_streaming")}
    return value


def expected_codex(S, M, options, *, build_kwargs=None, preflight=None):
    """Rebuild independently; Hermes imports occur only inside the isolated cell."""
    if build_kwargs is None:
        from agent.transports.codex import ResponsesApiTransport
        build_kwargs = ResponsesApiTransport().build_kwargs
    if preflight is None:
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs
        preflight = _preflight_codex_api_kwargs
    options = dict(options)
    sanitize = options.pop("sanitize_harmony_tokens", True)
    kwargs = build_kwargs(messages=[{"role": "system", "content": S}] + copy.deepcopy(M), **options)
    return preflight(kwargs, sanitize_harmony_tokens=sanitize)


def load_pinned_relay(provider_path):
    """Load only the run's provider copy, without executing plugin registration."""
    import importlib.util
    import sys
    from pathlib import Path
    from importlib import import_module
    root = Path(provider_path).resolve()
    if "profiles" in root.parts or Path.home() / ".hermes" in root.parents:
        raise ValueError("expectation requires the run's pinned provider copy")
    spec = importlib.util.spec_from_file_location("a4_pinned_relay", root / "__init__.py",
                                                submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    return import_module("a4_pinned_relay.directsdk"), import_module("a4_pinned_relay.admission")


def expected_relay(S, M, names, *, prepare_history=None, admission=None, reminder=None, provider_path=None):
    """Only a pinned provider copy is importable here; callers may inject pure fakes."""
    if prepare_history is None or admission is None:
        source, admission = load_pinned_relay(provider_path)
        prepare_history, reminder = source.prepare_history, source.SKILLS_REMINDER
    system, frames = prepare_history([{"role": "system", "content": S}] + copy.deepcopy(M), names)
    messages = [copy.deepcopy(frame["message"]) for frame in frames]
    queried = copy.deepcopy(messages[-1]["content"])
    if "skill_view" in names and not any(b.get("type") == "tool_result" for b in queried):
        messages[-1]["content"].append({"type": "text", "text": reminder})
    return {"system": [{"type": "text", "text": system}], "messages": messages,
            "_admission": admission, "_queried": queried}


def compare_request(expected, final, a4b):
    """Strict content equality; only route-recorded blocks may be inserted, in recorded positions."""
    item_dir = a4b.get("item_dir")
    def norm(value, system=False):
        if isinstance(value, str):
            return normalise(value, item_dir) if system else value.replace(str(item_dir), "<ITEM>") if item_dir else value
        if isinstance(value, list):
            return [norm(v, system) for v in value]
        if isinstance(value, dict):
            return {k: norm(v, system) for k, v in content_only(value).items()}
        return value
    final = content_only(final)
    if "instructions" in expected:
        return [k for k in ("instructions", "input") if norm(expected.get(k), k == "instructions")
                != norm(final.get(k), k == "instructions")]
    body = {k: copy.deepcopy(expected[k]) for k in ("system", "messages")}
    s_at, m_at, trailing = 0, len(expected["_queried"]), []
    user = body["messages"][-1]["content"]
    # The owner-approved PRIVATE record contains exact values; published placeholders are never used for checking.
    for block in a4b.get("blocks", []):
        kind, text, place = block["kind"], block["text"], block["placement"]
        part = {"type": "text", "text": text}
        if kind in {"S1", "S2"} and place == "system-prefix":
            body["system"].insert(s_at, part)
            s_at += 1
        elif kind == "M1" and place == "newest-user":
            user.insert(m_at, part)
            m_at += 1
        elif kind == "M1" and place == "trailing-system":
            trailing.append(text)
        else:
            return ["invalid_a4b"]
    for text in trailing:
        position = len(body["messages"])
        seen = final.get("messages", [])[position:position + 1]
        content = seen[0].get("content") if seen and seen[0].get("role") == "system" else None
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") == "text" and set(content[0]) == {"type", "text"}):
            text = [{"type": "text", "text": text}]
        body["messages"].append({"role": "system", "content": text})
    admission, queried = expected["_admission"], expected["_queried"]
    payload = canonical(body).encode()
    payload = admission.restore_queried_turn(payload, queried)
    payload = admission.stable_request_context(payload, queried, session=False)
    payload = admission.pin_message_breakpoint(payload, queried)
    payload = admission.stream_tool_input(payload)
    body = json.loads(payload)
    def equal(want, got):
        pattern = re.escape(canonical(want)).replace(re.escape("<date>"), r"\d{4}-\d{2}-\d{2}")
        pattern = pattern.replace(re.escape("<billing-value>"), r"[A-Za-z0-9._/+-]+")
        return re.fullmatch(pattern, canonical(got)) is not None
    return [k for k in ("system", "messages") if not equal(norm(body[k], k == "system"), norm(final.get(k), k == "system"))]


def message_component(message, item_dir=None):
    """A4 permits only the cell-directory replacement inside message content."""
    def replace(value):
        if isinstance(value, str):
            return value.replace(str(item_dir), "<ITEM>") if item_dir else value
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        return value
    # Codex represents a tool message's content as function_call_output.output.
    fields = {"content", "output"} if message.get("type") == "function_call_output" else {"content"}
    return component({k: replace(v) if k in fields else v for k, v in message.items()})


def capture_row(S, M, final, expected, a4b, item_dir=None):
    """Full captures stay in memory; only component receipts leave the process."""
    tools = final.get("tools") or []
    messages = final.get("input", final.get("messages", []))
    system = final.get("instructions", final.get("system", ""))
    return {"event": "capture", "system": component(normalise(S, item_dir)),
            "model": final.get("model"), "system_components": [component(v) for v in system] if isinstance(system, list) else [component(system)],
            "messages": [message_component(m, item_dir) for m in M], "final_system": component(normalise(system, item_dir)),
            "final_messages": [message_component(m, item_dir) for m in messages],
            "tool_schemas": {t.get("name", t.get("function", {}).get("name")): component(t)["sha256"] for t in tools},
            "checks": compare_request(expected, final, {**a4b, "item_dir": item_dir}),
            "canaries": {f"marker-{i}": hit for i, hit in enumerate(canaries(S, canonical(M), canonical(final)).values(), 1)}}


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
