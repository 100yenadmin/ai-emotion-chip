"""In-host driver for one Hermes-native listening cell. Runs under the acceptance venv's Python, sandboxed.

    python turn.py ARGS.json

ARGS.json: {"mode": "warm" | "turn" | "asr", "item_dir": ..., "prompt": ..., "clips": [...]}. The runner copies
this file and probe.py into the run dir and runs it from there with cwd at the run's empty cwd/ directory.
The last stdout line starting with PROOF= is the JSON result. Writes only inside the item dir.
"""
import json
import os
import shutil
import sys
from pathlib import Path

# In the run dir probe.py sits beside this file; in the repo it is bench_probe/probe.py.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "bench_probe")]
from probe import CHIP_SECTION, SKILL_ROW, canaries, digest, normalise, tool_names, upstream_row  # noqa: E402

TOOLSETS = ["tts", "skills"]
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


def wrap_upstream(item_dir):
    """Observe the exact body the DirectSDK relay forwards upstream (hashes and canaries only)."""
    found = []
    for name, module in list(sys.modules.items()):
        if name.endswith("admission") and hasattr(module, "pin_message_breakpoint") and hasattr(module, "Handler"):
            original = module.pin_message_breakpoint

            def pinned(payload, *a, _original=original, **k):
                result = _original(payload, *a, **k)
                try:
                    _append(Path(item_dir) / "upstream.jsonl", upstream_row(result, item_dir))
                except Exception as exc:  # never break the turn on a probe failure; the assert fails closed
                    _append(Path(item_dir) / "upstream.jsonl", {"event": "upstream_error", "error": type(exc).__name__})
                return result

            module.pin_message_breakpoint = pinned
            found.append(name)
    return found


def build_agent(cwd):
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
                    model=choice.model, max_iterations=MAX_ITERATIONS, enabled_toolsets=TOOLSETS, quiet_mode=True,
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
    agent, session_db, route = build_agent(args["cwd"])
    try:
        prompt = build_system_prompt(agent)
        names = tool_names(getattr(agent, "tools", None))
        return {"route": route, "system_sha256": digest(normalise(prompt, args["item_dir"])),
                "system_chars": len(normalise(prompt, args["item_dir"])), "chip_section": CHIP_SECTION in prompt,
                "skill_row": SKILL_ROW in prompt, "tools": names, "canaries": canaries(prompt)}
    finally:
        agent.close()
        if session_db is not None:
            session_db.close()


def turn(args):
    from hermes_cli.oneshot import _close_agent
    item_dir = args["item_dir"]
    sent = []
    (wrap_eleven if args.get("voice") == "ava" else wrap_fish)(item_dir, sent)
    relays = wrap_upstream(item_dir)
    agent, session_db, route = build_agent(args["cwd"])
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
            "failed": result.get("failed"), "error": result.get("error")}


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
