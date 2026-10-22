"""Heldout E2 gates and report-only E3 metrics with explicit completeness checks."""
import argparse
import json
from collections import Counter
from pathlib import Path
import re
import subprocess

from .gen import extract_reply, provenance
from .items import (ROOT, SETS, MODELS, CONDITIONS, RUNS, atomic_json, heldout_hashes,
                    load_items, read_json, run_directory, sha256)
from .judge import load_judgments, load_replies
from .score import difference, g3_checks, median_summary, rate, raw_features

SCOPE = "A3 (EVAL-PREREG amendment A3): the guardrail-free v1, not the gates frozen in prereg-v1"

CATEGORIES = ("strongly negative", "mildly negative", "neutral", "mildly positive", "strongly positive")


def prereg_slice(text):
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return b"".join(_before_amendments(raw.splitlines(keepends=True)))


def _before_amendments(lines):
    for line in lines:
        if line.rstrip(b"\r\n") == b"## Amendments":
            break
        yield line


def prereg_check(current=None, tagged=None):
    current = (ROOT / "EVAL-PREREG.md").read_bytes() if current is None else current
    if tagged is None:
        result = subprocess.run(["git", "show", "prereg-v1:EVAL-PREREG.md"], cwd=ROOT, capture_output=True)
        if result.returncode:
            return {"matches": False, "current_sha256": sha256(prereg_slice(current)),
                    "tag_sha256": None, "error": "prereg-v1 unavailable"}
        tagged = result.stdout
    actual, expected = sha256(prereg_slice(current)), sha256(prereg_slice(tagged))
    return {"matches": actual == expected, "current_sha256": actual, "tag_sha256": expected}


def prereg_bars(text):
    frozen = prereg_slice(text).decode("utf-8")
    match = re.search(r"ON minus OFF ≤ \+(\d+(?:\.\d+)?) percentage points", frozen)
    if not match:
        raise ValueError("cannot read G1 bar from prereg")
    return {"g1_margin": float(match[1]) / 100,
            "g1": "G1: report-only (scope A3)",
            "g2": "G2: retired (scope A3)",
            "g3": "G3: register_leak and consistency (ON, every set; scope A3)",
            "g1_claim_class": "no_large_regression_observed", "e3": "report-only; no bar"}


def identity(row, view=None):
    result = (row["set"], row["item"]["id"], row["model"], row["condition"], row["run"])
    return result + (view,) if view else result


def e3_metrics(rows, judgments):
    result = {}
    for model in MODELS:
        result[model] = {}
        for condition in CONDITIONS:
            cell = [r for r in rows if r["model"] == model and r["condition"] == condition]
            features = {identity(r): raw_features(r["reply"]) for r in cell}
            views = {}
            for view in ("with_cues", "stripped"):
                graded = [(r, judgments[identity(r, view)]) for r in cell if identity(r, view) in judgments]
                labels = [j for _, j in graded]
                views[view] = {
                    "neutral_share": rate(j["category"] == "neutral" for j in labels),
                    "categories": {name: rate(j["category"] == name for j in labels) for name in CATEGORIES},
                    "valence": {str(i): rate(j["valence"] == i for j in labels) for i in range(1, 8)},
                    "arousal": {str(i): rate(j["arousal"] == i for j in labels) for i in range(1, 8)},
                    "stance_present": rate(j["stance_present"] for j in labels),
                    "self_blame": rate(j["self_blame"] for j in labels),
                    "self_blame_items": [{"item": r["item"], "run": r["run"]} for r, j in graded if j["self_blame"]],
                }
            stripped = [(r, judgments[identity(r, "stripped")]) for r in cell if identity(r, "stripped") in judgments]
            result[model][condition] = {
                "views": views,
                "cue_rate": median_summary(count for f in features.values() for count in f["sentence_cue_counts"]),
                "untagged_neutral_items": rate(not features[identity(r)]["any_cue"] for r in cell if r["item"]["valence_hint"] == "neutral"),
                "untagged_neutral_work": rate(not features[identity(r)]["any_cue"] for r in cell if r["item"]["valence_hint"] == "neutral" and r["item"]["category"] == "work"),
                "fabrication_guard": rate(features[identity(r)]["high_positive"] for r, j in stripped if j["category"] == "neutral"),
                "expression_given_stance": rate(features[identity(r)]["any_cue"] for r, j in stripped if j["stance_present"]),
            }
    return result


def build_report(directory, tagged=None):
    directory = Path(directory)
    prereg = (ROOT / "EVAL-PREREG.md").read_bytes()
    verification, bars = prereg_check(prereg, tagged), prereg_bars(prereg)
    hashes, issues, rows, judgments = heldout_hashes(), [], [], {}
    expected, manifests = set(), {}
    for set_name in SETS:
        items = {item["id"]: item for item in load_items(set_name)}
        expected.update((set_name, item_id, model, condition, run) for item_id in items
                        for model in MODELS for condition in CONDITIONS for run in RUNS)
        loaded = load_replies(directory, set_name)
        manifest_path = directory / "manifests" / f"{set_name}.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            manifests[set_name] = manifest
            if manifest["heldout_sha256"] != hashes or manifest["guide_version"] != provenance()["guide_version"]:
                issues.append(f"{set_name}: generation input provenance changed")
            if any(item != items.get(item["id"]) for item in manifest["items"]):
                issues.append(f"{set_name}: manifest does not use canonical heldout items")
        elif loaded:
            issues.append(f"{set_name}: generation manifest missing")
        for row in loaded:
            if row["split"] != "heldout" or row["set"] != set_name or row["item"] != items.get(row["item"]["id"]):
                raise ValueError("report accepts canonical heldout replies only")
            if identity(row) not in expected or row["reply"] != extract_reply(row["raw"]):
                raise ValueError("invalid generation identity or raw output")
        rows.extend(loaded)
        if set_name in {"g1", "e3"}:
            judgments.update(load_judgments(directory, set_name, loaded))
    ids = [identity(row) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate generation identity")
    missing = sorted(expected - set(ids))
    missing_judgments = [identity(r, view) for r in rows if r["set"] in {"g1", "e3"}
                         for view in (("with_cues", "stripped") if r["set"] == "e3" else ("stripped",))
                         if identity(r, view) not in judgments]
    g1, g3 = {}, {}
    gate_failures = []
    for model in MODELS:
        g1[model] = {}
        for condition in CONDITIONS:
            cell = [r for r in rows if r["model"] == model and r["condition"] == condition]
            g1_j = [judgments[identity(r, "stripped")] for r in cell
                    if r["set"] == "g1" and identity(r, "stripped") in judgments]
            g1[model][condition] = {field: rate(j[field] for j in g1_j) for field in ("sycophantic", "harsh")}
        for condition in ("ON", "NATIVE"):
            for field in ("sycophantic", "harsh"):
                delta = difference(g1[model][condition][field], g1[model]["OFF"][field])
                g1[model][condition][field]["minus_OFF"] = delta
        checked = [{"set": r["set"], "item_id": r["item"]["id"], "run": r["run"],
                    "reasons": g3_checks(r["reply"])} for r in rows if r["model"] == model and r["condition"] == "ON"]
        groups = Counter((r["set"], r["item_id"]) for r in checked if not r["reasons"])
        g3[model] = {"failure_rate": rate(bool(r["reasons"]) for r in checked),
                     "failures": [r for r in checked if r["reasons"]],
                     "three_of_three": [{"set": s, "item_id": i["id"], "passed_runs": groups[(s, i["id"])],
                                         "pass": groups[(s, i["id"])] == len(RUNS)} for s in SETS for i in load_items(s)]}
        if g3[model]["failures"]:
            gate_failures.append(f"G3 {model}")
    gate_missing_judgments = [key for key in missing_judgments if key[0] != "e3"]
    complete = not (missing or gate_missing_judgments or issues)
    verdict = ("fail" if gate_failures else "pass") if verification["matches"] and complete else "unverified"
    return {"scope": SCOPE, "verdict": verdict, "prereg": verification, "bars": bars, "gate_complete": complete,
            "missing_generation": missing, "missing_judgments": missing_judgments, "integrity_issues": issues,
            "gate_failures": gate_failures, "g1": g1,
            "g3": g3, "e3": e3_metrics([r for r in rows if r["set"] == "e3"], judgments),
            "provenance": {s: {k: m[k] for k in ("guide_version", "prereg_sha256", "heldout_sha256")} for s, m in manifests.items()},
            "judge_validity": "Owner-labelled 20-item spot check must be supplied before release; not performed by this harness.",
            "ci_methods": {"rates": "Wilson 95%", "differences": "Newcombe independent Wilson 95%",
                           "median": "distribution-free sign interval, at least 95%; null endpoints if n too small"},
            "not_proven": "No inference about feelings, audio quality, or release readiness. E3 has no pass bar."}


def format_metric(metric):
    if metric["value"] is None:
        return "unavailable (n=0)"
    lo, hi = metric["ci95"]
    interval = f"[{lo:.4f}, {hi:.4f}]" if lo is not None else "unavailable"
    return f'{metric["value"]:.4f}; 95% CI {interval}' + (f'; n={metric["n"]}' if "n" in metric else "")


def markdown(report):
    lines = ["# M1b text behaviour bench", "", f'Scope: {report["scope"]}', "", f'Verdict: {report["verdict"]}', "",
             f'Prereg slice matches prereg-v1: {report["prereg"]["matches"]}',
             f'Gates complete: {report["gate_complete"]}; missing generations: {len(report["missing_generation"])}; missing judgments: {len(report["missing_judgments"])}.',
             "", "Intervals: Wilson for rates; Newcombe for differences; sign intervals for medians.", "",
             report["bars"]["g1"], report["bars"]["g2"], report["bars"]["g3"], ""]
    for model in MODELS:
        lines += [f"## {model}", "", "### G1", "", "| Condition | Outcome | Rate, CI, n | Difference vs OFF, CI |",
                  "| --- | --- | --- | --- |"]
        for condition in CONDITIONS:
            for outcome, metric in report["g1"][model][condition].items():
                delta = format_metric(metric["minus_OFF"]) if "minus_OFF" in metric else "baseline"
                lines.append(f"| {condition} | {outcome} | {format_metric(metric)} | {delta} |")
        lines += ["", "### G3 (ON, every set)", "", format_metric(report["g3"][model]["failure_rate"]), "",
                  "### E3 (report-only)", ""]
        for condition, metrics in report["e3"][model].items():
            lines += [f"#### {condition}", ""]
            for name in ("cue_rate", "untagged_neutral_items", "untagged_neutral_work", "fabrication_guard", "expression_given_stance"):
                lines.append(f"- {name}: {format_metric(metrics[name])}")
            for view, values in metrics["views"].items():
                for name in ("neutral_share", "stance_present", "self_blame"):
                    lines.append(f"- {view} {name}: {format_metric(values[name])}")
                for name in ("categories", "valence", "arousal"):
                    for bucket, metric in values[name].items():
                        lines.append(f"- {view} {name} {bucket}: {format_metric(metric)}")
            lines.append("")
    lines += ["## Failure details", "", "```json", json.dumps({
        "gate_failures": report["gate_failures"],
        "G3": {m: report["g3"][m]["failures"] for m in MODELS}}, indent=2), "```", "",
        "## Provenance and limits", "", "```json", json.dumps(report["provenance"], indent=2), "```", "",
        *report["integrity_issues"], report["judge_validity"], "", "Not proven: " + report["not_proven"], ""]
    return "\n".join(lines)


def write_report(directory, tagged=None):
    report = build_report(directory, tagged)
    atomic_json(Path(directory) / "report.json", report)
    (Path(directory) / "report.md").write_text(markdown(report), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="directory", type=run_directory, required=True)
    args = parser.parse_args(argv)
    write_report(args.directory)


if __name__ == "__main__":
    main()
