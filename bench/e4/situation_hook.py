"""Claude PostToolUse/PostToolUseFailure adapter; all counting and wording belong to the plugin."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time

if not __package__:
    # Run as a script inside the sandbox: import the plugin from this checkout, with no PYTHONPATH in the agent's env.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emotion_chip.hermes.hooks import Hooks  # noqa: E402
from emotion_chip.hermes.situation import Situation, situation_line  # noqa: E402

ARMS = ("none", "facts", "facts+affordance")
EVENTS = ("PostToolUse", "PostToolUseFailure")


def arm_code(salt, arm):
    """An opaque per-episode token for the arm. The agent can read the hook command, so it never names the arm."""
    return hashlib.sha256(f"{salt}:{arm}".encode()).hexdigest()[:16]


def arm_for(code, state):
    """The arm whose code matches, salted by the workspace id (the state file sits in <workspace>/state/)."""
    arm = next((arm for arm in ARMS if arm_code(state.parent.parent.name, arm) == code), None)
    if arm is None:
        raise SystemExit("unknown arm code")
    return arm


def status(event):
    """Claude Code 2.1.306 sends a failing tool (Bash non-zero exit included) to PostToolUseFailure. A Bash
    exit 1 from grep/rg/find/diff/test/[ still arrives as PostToolUse, with returnCodeInterpretation set; Hermes's
    terminal rule (non-zero exit) counts that as a failure too. Calls rejected at input validation fire no hook."""
    name = event.get("hook_event_name")
    if name not in EVENTS:
        raise ValueError("unexpected hook event")
    if name == "PostToolUseFailure":
        return "error"
    response = event.get("tool_response")
    if event.get("tool_name") == "Bash" and isinstance(response, dict) and response.get("returnCodeInterpretation"):
        return "error"
    return "ok"


def apply_event(event, state, arm, now=None):
    if arm not in ARMS:
        raise ValueError("unknown arm")
    result = status(event)
    now = time.time() if now is None else now
    situation = Situation(state, clock=lambda: now)
    hooks = Hooks({}, situation)
    session = event.get("session_id") or "episode"
    if "started_at" not in situation.session(session):
        hooks.on_session_start(session_id=session)
    hooks.post_tool_call(status=result, session_id=session)
    row = situation.session(session)
    if arm == "none" or row["tool_error_streak"] < 1:
        return None
    line = situation_line(row, now, affordance=arm == "facts+affordance")
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext": line}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--arm-code", required=True)
    args = parser.parse_args(argv)
    arm = arm_for(args.arm_code, args.state)
    event = json.load(sys.stdin)
    with args.state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(args.state.read_text()) if args.state.exists() else {}
        output = apply_event(event, state, arm)
        temporary = args.state.with_suffix(".part")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(args.state)
        # Event names only, for the episode receipt; never tool payloads.
        with args.state.with_name("events.log").open("a", encoding="utf-8") as log:
            log.write(f"{event['hook_event_name']} {event.get('tool_name')} {status(event)} "
                      f"streak={state['sessions'][event.get('session_id') or 'episode']['tool_error_streak']} "
                      f"emitted={output is not None}\n")
    if output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
