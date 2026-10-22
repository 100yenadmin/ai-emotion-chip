"""Offline command line entry points."""
import argparse
from dataclasses import asdict
import json
import sys

from .data import DIALECTS, load_data
from .guide import build_prompt_section, build_skill
from .render import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="emotion_chip")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render")
    render_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    render_cmd.add_argument("--model")
    render_cmd.add_argument("--json", action="store_true")
    render_cmd.add_argument("--no-sounds", dest="sounds", action="store_false", help="drop affect sounds (laughs, sighs)")
    render_cmd.add_argument("--allow-speakers", action="store_true", help="render [speaker: N] where the dialect has a marker")
    guide_cmd = commands.add_parser("guide")
    guide_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    guide_cmd.add_argument("--model", required=True)
    guide_cmd.add_argument("--skill", action="store_true")
    validate_cmd = commands.add_parser("validate-data")
    validate_cmd.add_argument("--path")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-data":
            load_data(args.path)
            print("data valid")
        elif args.command == "guide":
            builder = build_skill if args.skill else build_prompt_section
            print(builder(args.dialect, args.model, load_data()))
        else:
            result = render(sys.stdin.read(), args.dialect, vars(args))
            if args.json:
                print(json.dumps({"text": result.text, "params": result.params,
                                  "dropped": [asdict(x) for x in result.dropped], "notes": result.notes}))
            else:
                print(result.text)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
