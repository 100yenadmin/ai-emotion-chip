"""A4 text-only speech proxy. Paid execution is explicit; imports and injected tests are offline.

Run the parent with the pinned Hermes Python (its auth library needs that environment):
    <host>/.venv/bin/python -m bench.hermes.text --paths PATHS --scratch SCRATCH --out OUT
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time

from . import run as host
from .bench_probe import probe
from .. import gen
from ..items import BENCH, CONDITIONS, MODELS, RUNS, SETS, atomic_json, heldout_hashes, load_items, read_json, run_directory

SEED = 20261023
SKILL_TOOLS = frozenset({"skill_manage", "skill_view", "skills_list"})
HARNESS_ERRORS = frozenset({
    "credential lifetime is too short", "pinned provider copy differs",
    "Claude Code changed; a new route record is required", "pinned Claude Code binary changed",
    "golden warm failed", "golden arm deltas differ from published inputs",
    "STOP: credential or sign-in need", "harness parent must disable external login adoption",
})
HARNESS = ("Your final reply is spoken aloud as a voice message. There is no text_to_speech call in this "
           "conversation: write the reply exactly as it should be spoken.")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_paths(source):
    paths = read_json(Path(source).expanduser())
    keys = (set(host.PATH_KEYS) - {"host_label"}) | {"eleven_key_file", "claude_command", "harness_auth_home"}
    paths = {k: Path(v).expanduser() if k in keys and v else v for k, v in paths.items()}
    paths["canaries"] = [str(Path(c).expanduser()) if str(c).startswith("~") else str(c)
                         for c in paths.get("canaries", []) if str(c).strip()]
    return paths


def sections(model, arm):
    from emotion_chip.data import load_data
    from emotion_chip.guide import build_prompt_section
    from emotion_chip.hermes.registration import model_key
    result = {"bench-harness": HARNESS}
    if arm == "ON":
        result["emotion-chip"] = build_prompt_section("eleven_v4", model_key(model), load_data()).strip()
    if arm == "NATIVE":
        first, second = (BENCH / "native_guide.md").read_text().split("### Notes by voice model", 1)
        result.update({"bench-native-1": first.strip(), "bench-native-2": ("### Notes by voice model" + second).strip()})
    return result


def hermes_config(model, arm, claude_dir):
    if model not in MODELS or arm not in CONDITIONS:
        raise ValueError("unknown text cell route or arm")
    cfg = host.hermes_config("off", claude_dir)
    cfg.pop("stt")
    cfg["model"] = {"provider": host.PROVIDER if model == MODELS[0] else "openai-codex", "default": model}
    cfg["fallback_providers"] = []
    cfg["tts"] = {"provider": "emotion-chip", "model": "eleven_v4"}
    entries = {"emotion-chip": {"settings": {"backend": "eleven", "eleven_model": "eleven_v4",
               "signals": "off", "guide_mode": "generate" if arm == "ON" else "defer"}}}
    if model == MODELS[0]:
        entries[host.PROVIDER] = {"settings": {"claude_config_dir": str(claude_dir), "session_mode": False}}
    cfg["plugins"] = {"enabled": ["emotion-chip", "bench-probe", "bench-guidance"]
                      + ([host.PROVIDER] if model == MODELS[0] else []), "entries": entries}
    return cfg


def cell_order(pilot=False):
    units = [(s, item, r) for s in SETS for item in sorted(load_items(s), key=lambda i: i["id"])[:2 if pilot else None]
             for r in ((1,) if pilot else RUNS)]
    rng = random.Random(SEED)
    rng.shuffle(units)
    rows = []
    for s, item, r in units:
        arms = list(CONDITIONS)
        rng.shuffle(arms)
        rows.extend({"set": s, "split": "heldout", "item": item, "run": r, "condition": c} for c in arms)
    return rows


def safe_home(home):
    home = Path(home).resolve()
    if "profiles" in home.parts or home == Path.home() / ".hermes" or Path.home() / ".hermes" in home.parents:
        raise ValueError("cell credentials require an isolated harness home")
    return home


def public_a4b(records):
    public = {}
    for model, record in records.items():
        blocks = []
        for block in record.get("blocks", []):
            text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<account-email>", block["text"])
            text = re.sub(r"(Primary working directory: )[^\n]*", r"\1<cwd>", text)
            if block["kind"] == "S2":
                text = re.sub(r"([\w-]+=)[^;\s]+", r"\1<billing-value>", text)
            blocks.append({**block, "text": text})
        public[model] = {"blocks": blocks}
    return public


class TokenPair:
    """A parent-owned pair; refresh rotates once here, never independently in a cell."""
    def __init__(self, load, refresh, clock=time.time):
        self.load, self.refresh, self.clock = load, refresh, clock

    def provision(self, home):
        home = safe_home(home)
        pair = self.load()
        if pair["expires_at"] - self.clock() < 900:
            pair = self.refresh(pair)
        if pair["expires_at"] - self.clock() < 900:
            raise RuntimeError("credential lifetime is too short")
        value = {"version": 1, "active_provider": "openai-codex", "providers": {"openai-codex": {
            "auth_mode": "chatgpt", "tokens": {"access_token": pair["access_token"],
                                               "refresh_token": "harness-owned-no-refresh"}}}}
        fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)


def validity(result, golden, rows, memory_empty):
    captures = result.get("captures", [])
    count = result.get("api_calls", 0)
    complete = bool(count) and [r["api_call_count"] for r in captures] == list(range(1, count + 1))
    return {1: complete and all(not any(r["canaries"].values()) for r in captures),
            # A5: the compare_request result (r["checks"]) is recorded and reported; it does not fail check 2.
            2: complete and all(r["system"]["sha256"] == golden["system_sha256"]
                                    and r["model"] == golden["route"]["model"] for r in captures),
            3: complete and all(r["history_matches"] for r in captures)
                and result.get("guardrail") is None
                and result.get("skill_tree") is not None and result["skill_tree"] == golden.get("skill_tree"),
            5: memory_empty and host.turn_gate(result)["pass"] and bool(result.get("final_response", "").strip())}


TRANSPORT_IDS = ("metadata", "extra_headers", "prompt_cache_key")


def route_additions(request, item_dir, system_sha256):
    """A5: the final request minus the arm's S, transport ids, cell directory and dates; None unless one block is S."""
    key = "instructions" if "instructions" in request else "system"
    blocks = request.get(key) if isinstance(request.get(key), list) else [request.get(key)]
    hits = [i for i, b in enumerate(blocks) if probe.digest(probe.normalise(b, item_dir)) == system_sha256]
    if len(hits) != 1:
        return None
    rest = {k: v for k, v in request.items() if k not in TRANSPORT_IDS}
    if isinstance(request.get(key), list):
        rest[key] = blocks[:hits[0]] + blocks[hits[0] + 1:]
    else:
        rest.pop(key)
    def scrub(value):
        if isinstance(value, str):
            return re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", value.replace(str(item_dir), "<ITEM>"))
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value
    return probe.canonical(scrub(rest))


def run_phase(rows, execute, record, failure_count=0):
    """Immediate retries stay in place. Token-only retries form one deferred pass, using original medians."""
    originals, chosen, attempts, failures = [], {}, [], failure_count
    def attempt(index, row, number, median=None):
        nonlocal failures
        value = execute(row, number)
        value.update(index=index, attempt=number, row=row)
        checks = value["checks"]
        if median is not None:
            tokens = value.get("tokens")
            checks[4] = bool(median) and tokens is not None and abs(tokens - median) <= .05 * median
            value["median"] = median
        if any(not checks[k] for k in (1, 2, 3)):
            failures += 1
        attempts.append(value)
        if failures > 1:
            value["stopped"] = True
        return value
    try:
        stopped = False
        for index, row in enumerate(rows):
            first = attempt(index, row, 1)
            originals.append(first)
            chosen[index] = first
            if first.get("stopped"):
                stopped = True
                break
            if not all(first["checks"].values()):
                chosen[index] = attempt(index, row, 2)
                if chosen[index].get("stopped"):
                    stopped = True
                    break
        medians = {c: host.median(v["tokens"] for v in originals if v["row"]["condition"] == c
                                 and v.get("tokens") is not None) for c in CONDITIONS}
        for value in attempts:
            mid, tokens = medians[value["row"]["condition"]], value.get("tokens")
            value["median"] = mid
            value["checks"][4] = bool(mid) and tokens is not None and abs(tokens - mid) <= .05 * mid
        if not stopped:
            for index, first in enumerate(originals):
                if first["attempt"] == 1 and not first["checks"][4] and all(first["checks"][k] for k in (1, 2, 3, 5)):
                    chosen[index] = attempt(index, first["row"], 2, medians[first["row"]["condition"]])
                    if chosen[index].get("stopped"):
                        stopped = True
                        break
    except BaseException:
        for value in attempts:
            value.update(aborted=True, admitted=False)
            record(value)
        raise
    for value in attempts:
        value["admitted"] = chosen[value["index"]] is value and all(value["checks"].values())
        value["outlier"] = value["attempt"] == 2 and not value["checks"][4] and all(value["checks"][k] for k in (1, 2, 3, 5))
        record(value)
    return {"stopped": stopped, "attempts": attempts, "medians": medians, "critical_failures": failures}


def publish_attempt(out, phase, value):
    from datetime import datetime, timezone
    row = value["row"]
    path = Path(out) / "attempts" / phase / row["model"] / row["set"] / row["condition"] / row["item"]["id"]
    atomic_json(path / f'{row["run"]}-{value["attempt"]}.json', value)
    if value.get("aborted"):
        return
    if not value["admitted"]:
        log = gen.failures_path(out, row["set"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as handle:
            handle.write(json.dumps({"error": "InvalidCell", "message": "A4 validity failure", "phase": phase,
                "at": datetime.now(timezone.utc).isoformat(),
                "model": row["model"], "condition": row["condition"], "item_id": row["item"]["id"],
                "run": row["run"], "attempt": value["attempt"], "checks": value["checks"]}) + "\n")
    elif phase == "full":
        reply = value["reply"]
        atomic_json(gen.generation_path(out, row), {**row, "reply": reply, "raw": {"result": reply}})


class Harness:
    def __init__(self, scratch, out, paths, route_records, tokens, relay_token):
        self.run, self.out, self.paths = Path(scratch), Path(out), paths
        self.records, self.tokens, self.relay_token = route_records, tokens, relay_token
        self.host = host.Host(self.run, "", paths)
        self.golden, self.provenance = {}, {}

    def prepare(self):
        if not self.paths.get("canaries"):
            raise ValueError("text harness requires machine repository canaries")
        for sub in ("cwd", "src", "tools", "golden", "cells"):
            (self.run / sub).mkdir(parents=True)
        (self.run / "sandbox.sb").write_text(host.SANDBOX)
        for name in ("turn.py", "bench_probe/probe.py"):
            shutil.copyfile(host.HERE / name, self.run / "tools" / Path(name).name)
        chip = host.rev(self.paths["chip_repo"], host.CHIP_REF)
        host.archive(self.paths["chip_repo"], chip, self.run / "src/emotion-chip")
        for name, source in ((host.PROVIDER, self.paths["provider_src"]), ("bench-probe", host.HERE / "bench_probe")):
            shutil.copytree(source, self.run / "src" / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        guidance = self.run / "src/bench-guidance"
        guidance.mkdir()
        (guidance / "plugin.yaml").write_text("name: bench-guidance\nversion: 0.0.0\nkind: standalone\nmanifest_version: 2\napi_version: 1\n")
        (guidance / "__init__.py").write_text('import json\nfrom pathlib import Path\ndef register(ctx):\n'
            '    for key, text in json.loads(Path(__file__).with_name("sections.json").read_text()).items():\n'
            '        ctx.register_system_prompt_section(key, lambda info, text=text: text, max_chars=4000)\n')
        self.provenance = {"hermes_tree": host.tree_sha256(self.paths["host"], skip=host.HOST_SKIP),
            "provider_tree": host.tree_sha256(self.run / "src" / host.PROVIDER), "plugin_commit": chip,
            "plugin_tree": host.tree_sha256(self.run / "src/emotion-chip"),
            "bench_probe_tree": host.tree_sha256(self.run / "src/bench-probe"),
            "codex_route_sha256": probe.digest("".join((self.paths["host"] / p).read_text() for p in
                ("agent/transports/codex.py", "agent/codex_responses_adapter.py"))),
            "claude_code_version": self.paths["claude_code_version"],
            "normalization_sha256": probe.digest((host.HERE / "bench_probe/probe.py").read_text())}
        if self.provenance["provider_tree"] != host.tree_sha256(self.paths["provider_src"]):
            raise RuntimeError("pinned provider copy differs")
        command = str(Path(self.paths["claude_command"]).resolve())
        version = subprocess.run([command, "--version"], capture_output=True, text=True, check=True).stdout.split()[0]
        if version != self.paths["claude_code_version"]:
            raise RuntimeError("Claude Code changed; a new route record is required")
        self.provenance["claude_binary_sha256"] = file_sha256(command)
        self.paths["claude_command"] = command

    def home(self, item, model, arm, clone=None):
        if clone:
            shutil.copytree(clone / "home", item / "home", ignore=shutil.ignore_patterns("auth.json"))
        host.make_dirs(item)
        # Hermes' index builder returns early if this root is absent, even for plugin skills.
        (item / "home/skills").mkdir(exist_ok=True)
        for name in hermes_config(model, arm, item / "claude")["plugins"]["enabled"]:
            target = item / "home/plugins" / name
            if not target.exists():
                shutil.copytree(self.run / "src" / name, target)
        public = {k: v for k, v in sections(model, arm).items() if k != "emotion-chip"}
        (item / "home/plugins/bench-guidance/sections.json").write_text(json.dumps(public))
        (item / "home/config.yaml").write_text(json.dumps(hermes_config(model, arm, item / "claude")))
        if model == MODELS[1]:
            self.tokens.provision(item / "home")

    def call(self, item, model, arm, **extra):
        # Host's environment comes from an allowlist; the route override is also applied inside the isolated driver.
        args = {"mode": "turn", "text": True, "arm": arm, "item_dir": str(item), "cwd": str(self.run / "cwd"),
            "claude_command": self.paths.get("claude_command"),
            "provider_path": str(self.run / "src" / host.PROVIDER), "a4b": self.records.get(model, {}), **extra}
        if self.paths.get("claude_command") and file_sha256(self.paths["claude_command"]) != self.provenance["claude_binary_sha256"]:
            raise RuntimeError("pinned Claude Code binary changed")
        return self.host.call(item, args, token=self.relay_token() if model == MODELS[0] else None, timeout=600)

    def warm(self, model):
        for arm in CONDITIONS:
            item = self.run / "golden" / model / arm
            self.home(item, model, arm)
            result = self.call(item, model, arm, mode="warm", sections=sections(model, arm))
            db = host.db_summary(item / "home/state.db")
            sent = result.get("sent_tools")
            if (result.get("error") or db["sessions"] or db["usage_rows"]
                    or any(result.get("canaries", {}).values())
                    or sorted(result.get("tools", [])) != sorted(SKILL_TOOLS)
                    or not isinstance(sent, list) or len(sent) != len(result["tools"])
                    or any(not isinstance(name, str) or not name for name in sent)
                    or len(set(sent)) != len(sent)):
                raise RuntimeError("golden warm failed")
            result["canaries"] = {f"marker-{i}": hit for i, hit in enumerate(result["canaries"].values(), 1)}
            self.golden[model, arm] = result
        if len({self.golden[model, c]["residue"]["sha256"] for c in CONDITIONS}) != 1:
            raise RuntimeError("golden arm deltas differ from published inputs")

    def cell(self, phase, row, attempt):
        return self.run / "cells" / phase / row["model"] / row["set"] / row["condition"] / row["item"]["id"] / f'{row["run"]}-{attempt}'

    def execute(self, phase, row, attempt, route_record=False):
        model, arm = row["model"], row["condition"]
        item = self.cell(phase, row, attempt)
        self.home(item, model, arm, self.run / "golden" / model / arm)
        extra = {"route_record_path": str(self.run / f"{model}-{arm}-request.private.json")} if route_record else {}
        result = self.call(item, model, arm, prompt=row["item"]["user_message"], **extra)
        if host.credential_failure(result):
            raise RuntimeError("STOP: credential or sign-in need")
        rows = host.read_jsonl(item / "probe.jsonl")
        schemas = [r["tool_schemas"] for r in result.get("captures", [])]
        checks = validity(result, self.golden[model, arm], rows, host.memories_empty(item / "home"))
        checks[2] = checks[2] and all(set(s) == set(self.golden[model, arm]["sent_tools"]) for s in schemas)
        if checks[2] and schemas:
            reference = self.provenance.setdefault("tool_schemas", {}).setdefault(model, schemas[0])
            checks[2] = all(s == reference for s in schemas)
        return {"checks": checks,
                "reply": result.get("final_response", ""), "tokens": host.first_call_tokens(rows),
                "captures": result.get("captures", [])}

    def run_model(self, model, route_record=False):
        self.warm(model)
        if route_record:
            additions = {}
            for arm in CONDITIONS:
                neutral = {"model": model, "set": "neutral", "condition": arm, "run": 1,
                           "item": {"id": "neutral", "user_message": "Say hello."}}
                self.execute("route-record", neutral, 1, True)
                path = self.run / f"{model}-{arm}-request.private.json"
                rest = route_additions(read_json(path), self.cell("route-record", neutral, 1),
                                       self.golden[model, arm]["system_sha256"]) if path.exists() else None
                additions[arm] = None if rest is None else probe.digest(rest)
            return {"route_record": "PRIVATE owner inspection required", "additions_sha256": additions,
                    "route_arm_invariant": None not in additions.values() and len(set(additions.values())) == 1}
        identity = {k: v for k, v in self.provenance.items() if k != "tool_schemas"}
        if not self.records.get(model, {}).get("owner_approved") or self.records[model].get("identity") != identity:
            return {"missing": "owner-approved A4b required"}
        reference = {"set": "g1", "item": sorted(load_items("g1"), key=lambda i: i["id"])[0],
                     "condition": "OFF", "run": 1, "model": model, "split": "heldout"}
        receipts, failures = {}, 0
        for phase, rows in (("reference", [reference]), ("pilot", cell_order(True)), ("full", cell_order())):
            rows = [{**r, "model": model} for r in rows]
            receipts[phase] = run_phase(rows, lambda r, a: self.execute(phase, r, a),
                                       lambda v: publish_attempt(self.out, phase, v), failures)
            failures = receipts[phase]["critical_failures"]
            if receipts[phase]["stopped"] or (phase == "reference" and not any(
                v["admitted"] for v in receipts[phase]["attempts"])):
                break
        return receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=Path, required=True)
    parser.add_argument("--scratch", type=run_directory, required=True)
    parser.add_argument("--out", type=run_directory, required=True)
    parser.add_argument("--a4b", type=Path)
    parser.add_argument("--route-record", action="store_true")
    args = parser.parse_args(argv)
    paths = load_paths(args.paths)
    # Harness-owned store only: parent refresh uses the pinned Hermes library and an isolated config, never a CLI.
    auth_home = safe_home(paths["harness_auth_home"])
    os.environ["HERMES_HOME"] = str(auth_home)
    import sys
    sys.path.insert(0, str(paths["host"]))
    try:
        from hermes_cli.auth import _read_codex_tokens, resolve_codex_runtime_credentials
        from hermes_cli.config import load_config
    except ImportError as exc:
        raise SystemExit(f"Hermes imports unavailable ({exc}). Run with the pinned Hermes Python: "
                         f"{paths['host'] / '.venv/bin/python'} -m bench.hermes.text "
                         "--paths PATHS --scratch SCRATCH --out OUT") from exc
    if load_config().get("auth", {}).get("adopt_external_logins") is not False:
        raise RuntimeError("harness parent must disable external login adoption")
    def load_pair():
        import base64
        pair = _read_codex_tokens()["tokens"]
        payload = pair["access_token"].split(".")[1]
        expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        return {**pair, "expires_at": expiry}
    def refresh(pair):
        resolve_codex_runtime_credentials(refresh_skew_seconds=900)
        return load_pair()
    runner = Harness(args.scratch, args.out, paths, read_json(args.a4b) if args.a4b else {},
                     TokenPair(load_pair, refresh), gen.keychain_token)
    runner.prepare()
    records, model_failures = {}, {}
    for model in MODELS:
        try:
            records[model] = runner.run_model(model, args.route_record)
        except Exception as exc:
            message = str(exc) if type(exc) is RuntimeError and str(exc) in HARNESS_ERRORS else None
            model_failures[model] = {"error": type(exc).__name__, "message": message}
    extras = {"route_identities": runner.provenance, "a4b": runner.records, "phases": records,
              "model_failures": model_failures,
              "order": cell_order(), "pilot_order": cell_order(True), "seed": SEED,
              "golden": {f"{m}/{c}": v for (m, c), v in runner.golden.items()}}
    # Public A4b is supplied separately by the owner, with the required fixed placeholders.
    extras["a4b"] = public_a4b(runner.records)
    extras["validity_table"] = [v for phases in records.values() for name, phase in phases.items()
        if isinstance(phase, dict) and "attempts" in phase for v in phase["attempts"] if name == "full"]
    extras["cell_counts"] = {m: {"expected": 684, "admitted": sum(v["admitted"] for v in extras["validity_table"]
        if v["row"]["model"] == m)} for m in MODELS}
    for s in SETS:
        atomic_json(args.out / "manifests" / f"{s}.json", {"harness": "hermes", "dialect": "eleven_v4",
            "set": s, "split": "heldout", "items": load_items(s), "models": [m for m, r in records.items() if "full" in r],
            "conditions": list(CONDITIONS), "runs": list(RUNS), "heldout_sha256": heldout_hashes(),
            **gen.provenance(), **extras})


if __name__ == "__main__":
    main()
