"""In-host driver for one Hermes-native listening cell. Runs under the acceptance venv's Python, sandboxed.

    python turn.py ARGS.json

ARGS.json: {"mode": "warm" | "turn" | "asr", "item_dir": ..., "prompt": ..., "clips": [...]}. The runner copies
this file and probe.py into the run dir and runs it from there with cwd at the run's empty cwd/ directory.
The last stdout line starting with PROOF= is the JSON result. Writes only inside the item dir.
"""
import json
import copy
from functools import wraps
import os
import re
import shutil
import sys
from pathlib import Path

# In the run dir probe.py sits beside this file; in the repo it is bench_probe/probe.py.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "bench_probe")]
from probe import CHIP_SECTION, SKILL_ROW, canaries, digest, normalise, tool_names, upstream_row  # noqa: E402
from probe import canonical, capture_row, component, expected_codex, expected_relay  # noqa: E402

TOOLSETS = ["skills"]
LISTENING_TOOLSETS = ["tts", "skills"]
MAX_ITERATIONS = 6


def _append(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _recorder(item_dir, sent, original, prefix, row_of):
    """Wrap a synthesize(text, output_path, ...) call: record what it is handed and keep a copy of the audio."""
    def record(text, output_path, *a, **k):
        row = dict(row_of(k), text=text)
        sent.append(row)
        result = original(text, output_path, *a, **k)
        # Fish returns the written path; the ElevenLabs client returns its request id and writes output_path.
        source = Path(str(result)) if isinstance(result, (str, Path)) and Path(str(result)).is_file() else Path(output_path)
        if source.is_file():
            clip = Path(item_dir) / "out" / f"{prefix}-{len(sent)}{source.suffix or '.mp3'}"
            shutil.copyfile(source, clip)
            row["clip"] = clip.name
            row["bytes"] = clip.stat().st_size
        return result
    return record


def wrap_fish(item_dir, sent):
    """Record every text the fish-audio provider is handed and keep a copy of the mp3 it wrote."""
    from agent.tts_registry import get_provider
    fish = get_provider("fish-audio")
    if fish is None:
        raise RuntimeError("fish-audio TTS provider not registered")
    fish.synthesize = _recorder(item_dir, sent, fish.synthesize, "fish", lambda k: {
        "model": k.get("model"), "speed": k.get("speed"), "voice": k.get("voice"), "format": k.get("format")})


def wrap_eleven(item_dir, sent):
    """Record every text the emotion-chip provider's ElevenLabs client is handed and keep a copy of its audio."""
    from agent.tts_registry import get_provider
    chip = get_provider("emotion-chip")
    client = getattr(chip, "eleven", None)
    if client is None:
        raise RuntimeError("emotion-chip TTS provider or its ElevenLabs client not registered")
    client.synthesize = _recorder(item_dir, sent, client.synthesize, "eleven", lambda k: {
        "model": k.get("model"), "voice": k.get("voice"), "params": k.get("params"), "format": k.get("format")})


def wrap_upstream(item_dir, capture=None):
    """Observe the exact body the DirectSDK relay forwards upstream (hashes and canaries only)."""
    found = []
    for name, module in list(sys.modules.items()):
        if name.endswith("admission") and hasattr(module, "stream_tool_input") and hasattr(module, "Handler"):
            original = module.stream_tool_input

            @wraps(original)
            def pinned(payload, *a, _original=original, **k):
                result = _original(payload, *a, **k)
                try:
                    if capture:
                        capture(json.loads(result))
                    _append(Path(item_dir) / "upstream.jsonl", upstream_row(result, item_dir))
                except Exception as exc:  # never break the turn on a probe failure; the assert fails closed
                    _append(Path(item_dir) / "upstream.jsonl", {"event": "upstream_error", "error": type(exc).__name__})
                return result

            module.stream_tool_input = pinned
            found.append(name)
    if not found:
        raise RuntimeError("no relay final-request capture installed")
    return found


def build_agent(cwd, text=False):
    from hermes_cli.config import load_config
    from hermes_cli.oneshot import _create_session_db_for_oneshot, _resolve_model_and_provider
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback
    from hermes_constants import resolve_reasoning_config
    from run_agent import AIAgent

    cfg = load_config()
    choice = _resolve_model_and_provider(cfg, None, None)
    runtime, fallback = resolve_runtime_with_fallback(cfg, requested=choice.provider, target_model=choice.model or None,
                                                      explicit_base_url=None, explicit_api_key=None)
    if fallback is not None:
        raise RuntimeError("provider resolution fell back; refusing to run on another provider")
    session_db = _create_session_db_for_oneshot()
    agent = AIAgent(api_key=runtime.get("api_key"), base_url=runtime.get("base_url"), provider=runtime.get("provider"),
                    requested_provider=runtime.get("requested_provider"), api_mode=runtime.get("api_mode"),
                    model=choice.model, max_iterations=MAX_ITERATIONS,
                    enabled_toolsets=TOOLSETS if text else LISTENING_TOOLSETS, quiet_mode=True,
                    platform="telegram", session_db=session_db, credential_pool=runtime.get("credential_pool"),
                    request_overrides=runtime.get("request_overrides"),
                    reasoning_config=resolve_reasoning_config(cfg, choice.model), cwd=cwd)
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None
    return agent, session_db, {"model": choice.model, "provider": runtime.get("provider"),
                               "api_mode": runtime.get("api_mode"),
                               "reasoning": resolve_reasoning_config(cfg, choice.model)}


def stop_natives():
    """End the DirectSDK session-mode natives now: os._exit below skips the plugin's atexit shutdown."""
    for module in list(sys.modules.values()):
        shutdown = getattr(module, "_session_shutdown", None)
        if callable(shutdown) and hasattr(module, "NativeSession"):
            shutdown()


def tts_calls(messages):
    calls, results = [], []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            if function.get("name") != "text_to_speech":
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {"unparsed": True}
            calls.append({"text": arguments.get("text"), "arguments": sorted(arguments)})
        if message.get("role") == "tool" and message.get("name") in ("text_to_speech", None) \
                and "file_path" in str(message.get("content") or ""):
            try:
                content = json.loads(message.get("content"))
            except (TypeError, ValueError):
                content = {"raw": str(message.get("content"))[:500]}
            results.append({k: content[k] for k in ("success", "error", "provider", "voice_compatible")
                            if isinstance(content, dict) and k in content})
    return calls, results


def warm(args):
    from agent.system_prompt import build_system_prompt
    agent, session_db, route = build_agent(args["cwd"], text=args.get("text", False))
    try:
        prompt = build_system_prompt(agent)
        if agent.ephemeral_system_prompt:
            prompt = (prompt + "\n\n" + agent.ephemeral_system_prompt).strip()
        names = tool_names(getattr(agent, "tools", None))
        return {"route": route, "system_sha256": digest(normalise(prompt, args["item_dir"])),
                "system_chars": len(normalise(prompt, args["item_dir"])), "chip_section": CHIP_SECTION in prompt,
                "skill_row": SKILL_ROW in prompt, "tools": names, "canaries": canaries(prompt),
                **(golden_delta(agent, prompt, args) if args.get("text") else {})}
    finally:
        agent.close()
        if session_db is not None:
            session_db.close()


def golden_delta(agent, prompt, args):
    from agent.system_prompt import _frozen_plugin_prompt_sections, _skills_prompt
    from hermes_cli.plugins import format_system_prompt_sections
    sections = _frozen_plugin_prompt_sections(agent)
    bodies = {s.id: s.content for s in sections}
    if bodies != args["sections"]:
        raise RuntimeError("golden section bodies differ from published inputs")
    baseline = [s for s in sections if s.id == "bench-harness"]
    residue = prompt.replace(format_system_prompt_sections(sections), format_system_prompt_sections(baseline), 1)
    skills = _skills_prompt(agent)
    if args["arm"] == "ON":
        from hermes_cli.plugins import get_plugin_manager
        metadata = [m for m in get_plugin_manager().list_plugin_skill_metadata()
                    if m["name"] == "emotion-chip:emotion-chip-voice"]
        expected = [f'    - {m["name"]}: {str(m.get("description") or "").strip()}' for m in metadata]
        if len(expected) != 1 or skills.splitlines().count(expected[0]) != 1 \
                or len(re.findall(r"^\s+- \S+", skills, re.M)) != 1:
            raise RuntimeError("golden skill index differs")
        residue = residue.replace(skills + "\n\n", "", 1)
    elif skills:
        raise RuntimeError("unexpected golden skill index")
    skill = ""
    if args["arm"] == "ON":
        # The fresh cell has one profile scope: plugins_loader._directory_module_name
        # loads this directory plugin under hermes_plugins.emotion_chip.
        from hermes_plugins.emotion_chip.emotion_chip.hermes.settings import plugin_data_dir
        skill = (plugin_data_dir() / "skills/emotion-chip-voice/SKILL.md").read_text(encoding="utf-8")
    return {"residue": component(normalise(residue, args["item_dir"])),
            "skill_tree": skill_tree(),
            "sections": {s.id: component(s.content) for s in sections}, "skill_index": component(skills),
            "skill": component(skill)}


def skill_tree(home=None):
    """Hash both skill sources; skill_usage writes only these two root sidecars."""
    home = Path(home or os.environ["HERMES_HOME"])
    roots = (home / "skills", home / "plugin-data/emotion-chip/skills")
    files = []
    for index, root in enumerate(roots):
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if index == 0 and relative in {".usage.json", ".usage.json.lock"}:
                continue
            if path.is_file():
                files.append([index, relative, digest(path.read_bytes().hex())])
    return component(files)["sha256"]


def tool_reference(function, call_id, agent):
    """Normal read result, without skill_view's usage bump or repeat-view stub."""
    from tools import skills_tool
    from agent.tool_executor import _budget_for_agent, get_active_env
    from tools.tool_result_storage import maybe_persist_tool_result
    from agent.tool_dispatch_helpers import make_tool_result_message
    name = function.get("name")
    if name not in {"skills_list", "skill_view"}:
        return None
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(arguments, dict):
        # agent.tool_executor._parse_tool_arguments rejects these before dispatch.
        return None
    if name == "skills_list":
        result = skills_tool.skills_list(category=arguments.get("category"), task_id=agent._current_task_id)
    else:
        result = skills_tool.skill_view(arguments.get("name", ""), file_path=arguments.get("file_path"),
                                        task_id=agent._current_task_id)
    result = maybe_persist_tool_result(result, name, call_id,
        env=get_active_env(agent._current_task_id), config=_budget_for_agent(agent))
    return make_tool_result_message(name, agent._tool_result_content_for_active_model(name, result), call_id)


def tool_results_match(messages, agent):
    from agent.tool_executor import _budget_for_agent, get_active_env
    from tools.tool_result_storage import enforce_turn_budget
    calls, expected, actual, changed = {}, [], [], False
    def matches():
        enforce_turn_budget(expected, env=get_active_env(agent._current_task_id), config=_budget_for_agent(agent))
        return [m["content"] for m in expected] == [m.get("content") for m in actual]
    for message in messages:
        if message.get("role") == "assistant":
            if not matches():
                return False
            expected, actual = [], []
            for call in message.get("tool_calls", []):
                calls[call["id"]] = call["function"]
                changed |= call["function"].get("name") == "skill_manage"
        elif message.get("role") == "tool":
            if changed:
                return False
            reference = tool_reference(calls.get(message.get("tool_call_id"), {}), message.get("tool_call_id"), agent)
            if reference is None:
                return False
            expected.append(reference)
            actual.append(message)
    return matches()


def history_for_send(messages, agent):
    """The supported text/paired-tool send path, retaining whole tool-call dictionaries.

    Never replay api_content here: extra per-turn context must fail A4 check 3.
    Empty-turn/orphan repairs and thinking-only drops cannot form an accepted tool batch.
    Reasoning and persistence sidecars do not change the compared fields on this path.
    """
    from agent import conversation_loop
    from agent.message_sanitization import _sanitize_messages_surrogates
    wanted = copy.deepcopy(messages)
    for row in wanted:
        if agent._should_sanitize_tool_calls():
            agent._sanitize_tool_calls_for_strict_api(row, model=agent.model,
                                                     base_url=agent.base_url, provider=agent.provider)
        if isinstance(row.get("content"), str):
            row["content"] = row["content"].strip()
    conversation_loop._canonicalize_api_tool_calls(wanted)
    _sanitize_messages_surrogates(wanted)
    return wanted


def install_text_capture(args, agent):
    """Keep raw captures in process memory; preflight may run twice, so retain its last result per API call."""
    from agent import conversation_loop
    state, finals, accepted = {}, {}, {}
    original = conversation_loop.build_api_request
    @wraps(original)
    def build(subject, **kwargs):
        api = copy.deepcopy(kwargs["api_messages"])
        if not api or api[0].get("role") != "system":
            raise RuntimeError("missing pre-conversion system")
        S, M = api[0]["content"], api[1:]
        state.update(S=S, M=M, call=kwargs["api_call_count"],
                     history=copy.deepcopy(kwargs["messages"]), tools=copy.deepcopy(kwargs["tools_for_api"]))
        state["expected"] = expected_codex(S, M, {
            "model": agent.model, "provider": "openai-codex", "is_codex_backend": True,
            "replay_encrypted_reasoning": agent._codex_reasoning_replay_enabled}) if agent.provider == "openai-codex" else expected_relay(
                S, M, {n: n for n in tool_names(state["tools"])}, provider_path=args["provider_path"])
        return original(subject, **kwargs)
    conversation_loop.build_api_request = build
    def capture(final):
        row = capture_row(state["S"], state["M"], final, state["expected"], args.get("a4b", {}), args["item_dir"])
        row["api_call_count"] = state["call"]
        wanted = [{"role": "user", "content": args["prompt"]}] if state["call"] == 1 else history_for_send(state["history"], agent)
        fields = {"role", "content", "tool_calls", "tool_call_id"}
        project = lambda rows: [{k: v for k, v in m.items() if k in fields} for m in rows]
        row["history_matches"] = project(state["M"]) == project(wanted)
        if any(m.get("role") == "tool" for m in state["M"]):
            row["history_matches"] &= tool_results_match(state["M"], agent)
        prior = [n for n in accepted if n < state["call"]]
        current = project(state["M"])
        if prior:
            prefix = accepted[max(prior)]
            suffix = current[len(prefix):]
            row["history_matches"] &= current[:len(prefix)] == prefix and bool(suffix) and suffix[0]["role"] == "assistant"
            calls = suffix[0].get("tool_calls", []) if suffix else []
            row["history_matches"] &= bool(calls) and [c["id"] for c in calls] == [m.get("tool_call_id") for m in suffix[1:]]
        accepted[state["call"]] = current
        calls = {c["id"]: c["function"] for m in state["M"] for c in m.get("tool_calls", [])}
        row["skill_reads"] = [{**component(str(m.get("content") or "").replace(str(args["item_dir"]), "<ITEM>")),
                               "tool": calls.get(m.get("tool_call_id"), {}).get("name")}
                              for m in state["M"] if m.get("role") == "tool"]
        finals[state["call"]] = row
        if args.get("route_record_path"):
            fd = os.open(args["route_record_path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(canonical(final))
    if agent.provider == "openai-codex":
        from agent.transports.codex import ResponsesApiTransport
        preflight = ResponsesApiTransport.preflight_kwargs
        @wraps(preflight)
        def final_kwargs(transport, *a, **k):
            result = preflight(transport, *a, **k)
            capture(result)
            return result
        ResponsesApiTransport.preflight_kwargs = final_kwargs
        installed = ["ResponsesApiTransport.preflight_kwargs"]
    else:
        installed = wrap_upstream(args["item_dir"], capture)
    return finals, installed


def turn(args):
    from hermes_cli.oneshot import _close_agent
    item_dir = args["item_dir"]
    sent = []
    text = args.get("text", False)
    if not text:
        (wrap_eleven if args.get("voice") == "ava" else wrap_fish)(item_dir, sent)
    agent, session_db, route = build_agent(args["cwd"], text=text)
    if args.get("route_record_path"):
        agent.max_iterations = 1
    captures, relays = install_text_capture(args, agent) if text else ({}, wrap_upstream(item_dir))
    relay_session_id = getattr(agent, "session_id", None)
    try:
        result = agent.run_conversation(args["prompt"])
    finally:
        _close_agent(agent, session_db, relay_session_id)
    stop_natives()
    calls, results = tts_calls(result.get("messages"))
    renders = []
    for name, module in list(sys.modules.items()):
        if name.endswith(".hermes.provider") and hasattr(module, "last_render"):
            renders = [dict(v) for v in module.last_render.values()]
    return {"route": route, "relays": relays, "final_response": result.get("final_response") or "",
            "tts_calls": calls, "tts_results": results, "sent_to_voice": sent, "last_render": renders,
            "api_calls": result.get("api_calls"), "completed": result.get("completed"),
            "failed": result.get("failed"), "error": result.get("error"), "guardrail": result.get("guardrail"),
            **({"skill_tree": skill_tree()} if text else {}),
            "captures": list(captures.values())}


def asr(args):
    from tools.transcription_tools import transcribe_audio
    rows = {}
    for clip in args["clips"]:
        out = transcribe_audio(clip)
        rows[Path(clip).name] = {"success": out.get("success"), "transcript": out.get("transcript") or "",
                                 "error": out.get("error")}
    return rows


def main():
    args = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope(None):
        discover_plugins()
        result = {"warm": warm, "turn": turn, "asr": asr}[args["mode"]](args)
    print("PROOF=" + json.dumps(result, default=str), flush=True)
    sys.stdout.flush()
    # Session-mode natives and relay threads must not hold the sandboxed process open.
    os._exit(0)


if __name__ == "__main__":
    main()
