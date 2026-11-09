"""Offline command line entry points: `python -m emotion_chip` and, inside Hermes, `hermes chip`."""
import argparse
from dataclasses import asdict
import json
import sys

from .data import DATA_ROOT, DIALECTS, build_bundle, check_bundle, load_data
from .guide import build_prompt_section, build_skill
from .render import render


def add_commands(parser):
    """Add the subcommands to *parser*. Hermes calls this with the `hermes chip` parser."""
    # Not "command": Hermes keeps its own top-level choice in args.command.
    commands = parser.add_subparsers(dest="chip_command", metavar="COMMAND", required=True)
    render_cmd = commands.add_parser("render", help="render a delivery score for one voice engine",
                                     description="Render a delivery score for one voice engine. "
                                                 "The score comes from the argument, else from stdin. "
                                                 "This offline command renders the score as written; the speech "
                                                 "provider and chip_preview apply Fish's paren-cue adaptation "
                                                 "on the Fish backend.")
    render_cmd.add_argument("text", nargs="?", help='the score, e.g. "[whisper] It passed."')
    render_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    render_cmd.add_argument("--model")
    render_cmd.add_argument("--json", action="store_true", help="print text, parameters, drops and notes")
    render_cmd.add_argument("--no-sounds", dest="sounds", action="store_false", help="drop affect sounds (laughs, sighs)")
    render_cmd.add_argument("--allow-speakers", action="store_true", help="render [speaker: N] where the dialect has a marker")
    guide_cmd = commands.add_parser("guide", help="print the delivery guide the agent gets",
                                    description="Print the prompt section (or, with --skill, the skill) for a "
                                                "dialect and model.")
    guide_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    guide_cmd.add_argument("--model", required=True)
    guide_cmd.add_argument("--skill", action="store_true")
    validate_cmd = commands.add_parser("validate-data", help="check the emotion data files")
    validate_cmd.add_argument("--path")
    for name in ("build-bundle", "check-bundle"):
        commands.add_parser(name, help=f"{name.split('-')[0]} the packaged data bundle").add_argument("--path")
    return parser


def run(args):
    """Run the parsed subcommand; returns the exit status."""
    command = args.chip_command
    try:
        if command == "validate-data":
            load_data(args.path or DATA_ROOT)
            print("data valid")
        elif command in {"build-bundle", "check-bundle"}:
            (build_bundle if command == "build-bundle" else check_bundle)(args.path)
            print("bundle built" if command == "build-bundle" else "bundle current")
        elif command == "guide":
            builder = build_skill if args.skill else build_prompt_section
            print(builder(args.dialect, args.model, load_data()))
        else:
            if args.text is not None:
                text = args.text
            elif sys.stdin is None or sys.stdin.isatty():
                print('give the score as an argument, e.g. render --dialect eleven_v4 "[whisper] It passed.", '
                      "or pipe it in", file=sys.stderr)
                return 2
            else:
                text = sys.stdin.read()
            result = render(text, args.dialect, vars(args))
            if args.json:
                print(json.dumps({"text": result.text, "params": result.params,
                                  "dropped": [asdict(x) for x in result.dropped], "notes": result.notes}))
            else:
                print(result.text)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    return run(add_commands(argparse.ArgumentParser(prog="emotion_chip")).parse_args(argv))
