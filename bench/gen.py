"""Isolated three-run text generation. Importing this module accesses no credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from emotion_chip.data import load_data
from emotion_chip.guide import build_prompt_section, build_skill
from .items import (BENCH, ROOT, SETS, MODELS, CONDITIONS, RUNS, atomic_json,
                    heldout_hashes, load_items, read_json, run_directory, sha256)


def system_prompt(model, condition):
    base = (BENCH / "base_system.md").read_text(encoding="utf-8").strip()
    if condition == "OFF":
        return base
    if condition == "NATIVE":
        extra = (BENCH / "native_guide.md").read_text(encoding="utf-8").strip()
    elif condition == "ON":
        data = load_data()
        extra = build_prompt_section("fish_s2", model, data) + "\n\n" + build_skill("fish_s2", model, data)
    else:
        raise ValueError("unknown condition")
    return base + "\n\n" + extra


def isolated_environment(token, config, environ=None):
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k != "CLAUDECODE"
           and not k.startswith("CLAUDE_CODE_")
           and k not in {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}}
    env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_OAUTH_TOKEN=token)
    return env


def keychain_token():
    """Called only by an explicitly invoked live generator; never log subprocess output."""
    result = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials",
                             "-a", os.environ.get("USER", ""), "-w"], capture_output=True, text=True)
    try:
        token = json.loads(result.stdout)["claudeAiOauth"]["accessToken"]
        if result.returncode or not isinstance(token, str) or not token:
            raise ValueError
        return token
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Claude subscription credential unavailable") from None


def extract_reply(raw):
    if not isinstance(raw, dict) or raw.get("is_error") or not isinstance(raw.get("result"), str) or not raw["result"].strip():
        raise ValueError("generation did not return a successful text result")
    return raw["result"]


def generate_reply(model, prompt, user_message, runner=None, token_loader=None):
    runner = runner or subprocess.run
    token_loader = token_loader or keychain_token
    with tempfile.TemporaryDirectory(prefix="emotion-chip-gen-") as temp:
        config = Path(temp)
        (config / "settings.json").write_text("{}", encoding="utf-8")
        system = config / "system.md"
        system.write_text(prompt, encoding="utf-8")
        command = ["claude", "-p", "--model", model, "--system-prompt-file", str(system),
                   "--tools", "", "--max-turns", "1", "--output-format", "json"]
        # The working directory is the empty temp dir: claude -p loads instruction files from the working
        # directory's parent folders, which voided run-1 (evidence/run-1).
        result = runner(command, input=user_message, text=True, capture_output=True, cwd=temp,
                        env=isolated_environment(token_loader(), config), timeout=600)
        if result.returncode:
            raise RuntimeError("generation failed; subprocess diagnostics suppressed")
        try:
            raw = json.loads(result.stdout)
            extract_reply(raw)
        except (ValueError, TypeError):
            raise RuntimeError("generation returned invalid output") from None
        return raw


def generation_path(out, row):
    return Path(out) / "generation" / row["set"] / row["model"] / row["condition"] / row["item"]["id"] / f'{row["run"]}.json'


def provenance():
    paths = sorted(p for p in (ROOT / "emotion_chip" / "data").rglob("*") if p.is_file())
    return {"guide_version": sha256("".join(p.relative_to(ROOT).as_posix() + sha256(p.read_bytes()) for p in paths)),
            "prereg_sha256": sha256((ROOT / "EVAL-PREREG.md").read_bytes())}


def generate(set_name, out, models=MODELS, conditions=CONDITIONS, limit=None, concurrency=4, generate_fn=None):
    if not 1 <= concurrency <= 4 or (limit is not None and limit < 1):
        raise ValueError("concurrency must be 1..4 and limit must be positive")
    if not models or len(set(models)) != len(models) or any(m not in MODELS for m in models):
        raise ValueError("unsupported or duplicate models")
    if not conditions or len(set(conditions)) != len(conditions) or any(c not in CONDITIONS for c in conditions):
        raise ValueError("unsupported or duplicate conditions")
    hashes = heldout_hashes()
    items = load_items(set_name)[:limit]
    prompts = {m: {c: system_prompt(m, c) for c in conditions} for m in models}
    manifest = {"set": set_name, "split": "heldout", "models": list(models), "conditions": list(conditions),
                "runs": list(RUNS), "items": items, "heldout_sha256": hashes, "prompts": prompts,
                "harness": "claude-p", **provenance()}
    path = Path(out) / "manifests" / f"{set_name}.json"
    if path.exists():
        if read_json(path) != manifest:
            raise ValueError("resume inputs or prompts changed")
    else:
        atomic_json(path, manifest)
    generate_fn = generate_fn or generate_reply

    def one(row):
        target = generation_path(out, row)
        if target.exists():
            previous = read_json(target)
            if any(previous[k] != v for k, v in row.items()):
                raise ValueError("existing output identity mismatch")
            if previous["reply"] != extract_reply(previous["raw"]):
                raise ValueError("existing output is inconsistent")
            return
        raw = generate_fn(row["model"], prompts[row["model"]][row["condition"]], row["item"]["user_message"])
        atomic_json(target, {**row, "reply": extract_reply(raw), "raw": raw})

    rows = [{"set": set_name, "split": "heldout", "model": m, "condition": c, "item": item, "run": run}
            for item in items for m in models for c in conditions for run in RUNS]
    # Stop at the first failure (an expired credential, a hung CLI): no queued call starts after it, calls already
    # running finish, and finished outputs stay on disk for a resume.
    failed = threading.Event()

    def guarded(row):
        if failed.is_set():
            return
        try:
            one(row)
        except BaseException:
            failed.set()
            raise

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(guarded, row) for row in rows]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=MODELS)
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS, default=CONDITIONS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=run_directory, required=True)
    args = parser.parse_args(argv)
    generate(args.set, args.out, args.models, args.conditions, args.limit, args.concurrency)


if __name__ == "__main__":
    main()
