"""Offline command line entry points."""
import argparse
from dataclasses import asdict
import json
import sys

from .data import DATA_ROOT, DIALECTS, build_bundle, check_bundle, load_data
from .guide import build_prompt_section, build_skill
from .render import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="emotion_chip")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render")
    render_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    render_cmd.add_argument("--model")
    render_cmd.add_argument("--json", action="store_true")
    render_cmd.add_argument("--sounds-per-turn", type=int, default=1)
    guide_cmd = commands.add_parser("guide")
    guide_cmd.add_argument("--dialect", required=True, choices=DIALECTS)
    guide_cmd.add_argument("--model", required=True)
    guide_cmd.add_argument("--skill", action="store_true")
    validate_cmd = commands.add_parser("validate-data")
    validate_cmd.add_argument("--path")
    for name in ("build-bundle", "check-bundle"):
        commands.add_parser(name).add_argument("--path")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-data":
            load_data(args.path or DATA_ROOT)
            print("data valid")
        elif args.command in {"build-bundle", "check-bundle"}:
            (build_bundle if args.command == "build-bundle" else check_bundle)(args.path)
            print("bundle built" if args.command == "build-bundle" else "bundle current")
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
