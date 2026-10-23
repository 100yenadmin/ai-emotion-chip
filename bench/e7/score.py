"""E7 locked-collection scorer. Returns counts only, never notes, prompts, replies or paths."""
from datetime import datetime
import math
from pathlib import Path
import statistics

from bench import report
from .session import attempt_counts, prompts, read, score_receipt_path, sha, write


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing ISO timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result


def binomial(successes, n):
    return sum(math.comb(n, k) for k in range(successes, n + 1)) / 2**n


def score(ratings, key, manifest):
    if manifest.get("collection") != key.get("collection"):
        raise ValueError("ratings collection identity mismatch")
    docs = ratings.get("docs")
    if not isinstance(docs, dict):
        raise ValueError("expected a docs object")
    if "lock" not in docs:
        return {"status": "incomplete"}
    locked = timestamp(docs["lock"].get("lockedAt"))
    trials = key["trials"]
    ids = [t["id"] for t in trials]
    if len(ids) != len(set(ids)) or set(docs) - set(ids) - {"lock"}:
        raise ValueError("duplicate or unknown trial")
    data = prompts()
    for comparison in ("comparison_a", "comparison_b"):
        items = [t["item"] for t in trials if t["kind"] == "real" and t["comparison"] == comparison]
        if len(items) != 20 or set(items) != set(data[comparison]):
            raise ValueError("key is missing real trials")
    catch_items = data["catch_reserve" if key.get("rerun_of") else "catch"]
    for kind, expected in (("catch", catch_items), ("mini", data["mini_sessions"])):
        items = [t["item"] for t in trials if t["kind"] == kind]
        if len(items) != len(expected) or set(items) != set(expected):
            raise ValueError("key is missing auxiliary trials")
    incomplete = False
    for trial in trials:
        doc = docs.get(trial["id"])
        if doc is None:
            incomplete = True
            continue
        if doc.get("trial") != trial["id"]:
            raise ValueError("trial doc identity mismatch")
        if timestamp(doc.get("updatedAt")) > locked:
            raise ValueError("rating was changed after lock")
        if not isinstance(doc.get("note"), str) or not isinstance(doc.get("acting"), dict) or any(
                type(doc["acting"].get(side)) is not bool for side in ("a", "b")):
            raise ValueError("invalid rating document")
        if trial["kind"] == "mini":
            if type(doc.get("rating")) is not int or not 1 <= doc["rating"] <= 5:
                incomplete = True
        elif doc.get("choice") not in {"a", "b", "none"}:
            incomplete = True
    catches = sum(docs.get(t["id"], {}).get("choice") in {"a", "b"} for t in trials if t["kind"] == "catch")
    if catches > 1:
        return {"status": "void", "catch_preferences": catches}
    if incomplete:
        return {"status": "incomplete"}
    comparisons = {}
    for comparison in ("comparison_a", "comparison_b"):
        rows = [t for t in trials if t["comparison"] == comparison and t["kind"] == "real"]
        preferred = "cued" if comparison == "comparison_a" else "chip"
        wins = sum(t["sides"].get(docs[t["id"]]["choice"]) == preferred for t in rows)
        acting = {}
        for trial in rows:
            for side, arm in trial["sides"].items():
                acting[arm] = acting.get(arm, 0) + docs[trial["id"]]["acting"][side]
        comparisons[comparison] = {"chip_preferred": wins, "n": len(rows), "pass": wins >= 15,
                                   "p_one_sided": binomial(wins, len(rows)), "acting": acting}
    passed = all(v["pass"] for v in comparisons.values())
    if passed and not report.prereg_check()["matches"]:
        raise ValueError("pass refused: preregistration does not match")
    mini = [docs[t["id"]]["rating"] for t in trials if t["kind"] == "mini"]
    result = {"status": "pass" if passed else "fail", "comparisons": comparisons,
              "attempt_counts": attempt_counts(manifest),
              "catch_preferences": catches, "notes": sum(bool(docs[t["id"]]["note"].strip()) for t in trials),
              "mini_ratings": mini, "mini_median": statistics.median(mini)}
    if passed and not key.get("dry_run"):
        result["claim_class"] = "owner_accepted(n=1)"
    if key.get("dry_run"):
        result["dry_run"] = True
    return result


def score_files(ratings_path, key_path, manifest_path):
    manifest, key = read(manifest_path), read(key_path)
    if sha(key_path) != manifest.get("key_sha256"):
        raise ValueError("key commitment mismatch")
    ratings = read(ratings_path)
    result = score(ratings, key, manifest)
    if "lock" in ratings["docs"]:
        lock = ratings["docs"]["lock"]["lockedAt"]
        if manifest.get("locked_at") and (manifest["locked_at"] != lock or
                                          manifest["ratings_sha256"] != sha(ratings_path)):
            raise ValueError("locked ratings changed")
        manifest.update(locked_at=lock, ratings_sha256=sha(ratings_path), score=result)
        write(manifest_path, manifest)
    write(score_receipt_path(key_path), {"key_sha256": sha(key_path), "status": result["status"]})
    return result
