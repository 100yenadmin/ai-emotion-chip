"""E7 session CLI. Generation is paid unless --dry-run is explicitly supplied."""
import argparse
import json

from .session import build, generate
from .score import score_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--tag", required=True)
    gen.add_argument("--session", required=True)
    gen.add_argument("--dry-run", action="store_true")
    gen.add_argument("--retry-infra")
    gen.add_argument("--reason")
    bundle = commands.add_parser("build")
    bundle.add_argument("--session", required=True)
    bundle.add_argument("--bundle", required=True)
    bundle.add_argument("--key", required=True)
    bundle.add_argument("--rerun-of")
    scoring = commands.add_parser("score")
    scoring.add_argument("--ratings", required=True)
    scoring.add_argument("--key", required=True)
    scoring.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    if args.command == "generate":
        generate(args.tag, args.session, dry_run=args.dry_run, retry_infra=args.retry_infra, reason=args.reason)
        print("generation recorded")
    elif args.command == "build":
        build(args.session, args.bundle, args.key, rerun_of=args.rerun_of)
        print("bundle and private key recorded")
    else:
        print(json.dumps(score_files(args.ratings, args.key, args.manifest), sort_keys=True))


if __name__ == "__main__":
    main()
