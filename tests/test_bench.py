"""Offline harness acceptance: all subprocesses and model answers are fakes."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import gen, items, judge, report, score
from emotion_chip.data import load_data
from emotion_chip.guide import build_prompt_section, build_skill


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external processes, including models and keychain, are forbidden in unit tests")
    monkeypatch.setattr(gen.subprocess, "run", forbidden)


def row(set_name="g1", condition="ON", run=1, model=items.MODELS[0], reply="[firm] That is incorrect."):
    return {"set": set_name, "split": "heldout", "model": model, "condition": condition,
            "run": run, "item": items.load_items(set_name)[0], "reply": reply,
            "raw": {"result": reply}}


def fake_labels(set_name, opaque):
    if set_name == "g1":
        return {"id": opaque, "sycophantic": False, "harsh": False, "note": ""}
    return {"id": opaque, "category": "neutral", "valence": 4, "arousal": 4,
            "stance_present": True, "self_blame": False}


def fake_judge(prompt_path, schema_path, output_path):
    batch = json.loads(prompt_path.read_text().split("\n\n")[-1])
    return {"results": [fake_labels(schema_path.stem, entry["id"]) for entry in batch]}


def test_fixed_items_and_hashes():
    assert len(items.heldout_hashes()) == 76
    for name, count in {"g1": 20, "g2": 10, "g3": 6, "e3": 40}.items():
        assert len(items.load_items(name)) == count
    for name, count in {"g1": 6, "g2": 4, "e3": 8}.items():
        assert len(items.load_items(name, "tune")) == count
    g1, g2, e3 = (items.load_items(name) for name in ("g1", "g2", "e3"))
    assert sum(i["framing"] == "emotional" for i in g1) == 10
    assert sum(i["risk_cues"] for i in g2) == 6
    assert sum(i["category"] == "work" for i in e3) == 20


@pytest.mark.parametrize("set_name,field,value", [
    ("g1", "framing", "sad"), ("g1", "false_claim", ""),
    ("g2", "risk_cues", "false"), ("g2", "risk_cues", 1),
    ("g3", "hazard", []), ("e3", "category", "job"),
    ("e3", "valence_hint", "happy"), ("g1", "id", "../../escape"),
])
def test_item_schema_negative_controls(set_name, field, value):
    item = deepcopy(items.load_items(set_name)[0])
    item[field] = value
    with pytest.raises(ValueError):
        items.validate_item(item, set_name)


def test_missing_extra_fields_and_filename(tmp_path):
    item = deepcopy(items.load_items("g1")[0])
    del item["framing"]
    with pytest.raises(ValueError):
        items.validate_item(item, "g1")
    item = deepcopy(items.load_items("g1")[0])
    item["extra"] = "unexpected"
    with pytest.raises(ValueError):
        items.validate_item(item, "g1")
    directory = tmp_path / "g1" / "heldout"
    directory.mkdir(parents=True)
    (directory / "wrong.yaml").write_text(json.dumps(items.load_items("g1")[0]))
    with pytest.raises(ValueError, match="filename"):
        items.load_items("g1", root=tmp_path)


def test_heldout_manifest_detects_change(tmp_path):
    path = tmp_path / "g1" / "heldout"
    path.mkdir(parents=True)
    (path / "test.yaml").write_text("changed")
    (tmp_path / "HELDOUT.sha256").write_text("deadbeef  ./g1/heldout/test.yaml\n")
    with pytest.raises(ValueError, match="HELDOUT"):
        items.heldout_hashes(tmp_path)


def test_conditions_exactly_use_existing_inputs():
    data = load_data()
    base = (items.BENCH / "base_system.md").read_text().strip()
    assert gen.system_prompt(items.MODELS[0], "OFF") == base
    assert gen.system_prompt(items.MODELS[0], "NATIVE") == base + "\n\n" + (items.BENCH / "native_guide.md").read_text().strip()
    for model in items.MODELS:
        expected = base + "\n\n" + build_prompt_section("fish_s2", model, data) + "\n\n" + build_skill("fish_s2", model, data)
        assert gen.system_prompt(model, "ON") == expected


def test_isolated_generation_with_fake_runner():
    seen = []
    def fake(command, **kwargs):
        env = kwargs["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-test-token"
        assert Path(env["CLAUDE_CONFIG_DIR"], "settings.json").read_text() == "{}"
        assert Path(command[command.index("--system-prompt-file") + 1]).read_text() == "system text"
        assert command == ["claude", "-p", "--model", items.MODELS[0], "--system-prompt-file", command[5],
                           "--tools", "", "--max-turns", "1", "--output-format", "json"]
        assert kwargs["input"] == "user text"
        assert kwargs["cwd"] == env["CLAUDE_CONFIG_DIR"]
        seen.append(Path(env["CLAUDE_CONFIG_DIR"]))
        return SimpleNamespace(returncode=0, stdout='{"result": "fake reply"}')
    raw = gen.generate_reply(items.MODELS[0], "system text", "user text", fake, lambda: "fake-test-token")
    assert raw["result"] == "fake reply"
    assert not seen[0].exists()
    env = gen.isolated_environment("fake", "config", {
        "CLAUDECODE": "1", "CLAUDE_CODE_NEW_MARKER": "1", "ANTHROPIC_BASE_URL": "fake",
        "ANTHROPIC_AUTH_TOKEN": "fake", "ANTHROPIC_API_KEY": "fake", "PATH": "retained"})
    assert set(env) == {"PATH", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


@pytest.mark.parametrize("returncode,stdout", [(1, "sensitive diagnostic"), (0, "not json"),
                                               (0, '{"is_error":true,"result":"error"}'), (0, '{"result":""}')])
def test_generation_errors_suppress_diagnostics(returncode, stdout):
    def fake(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout)
    with pytest.raises(RuntimeError) as exc:
        gen.generate_reply(items.MODELS[0], "sys", "user", fake, lambda: "fake")
    assert stdout not in str(exc.value)


def test_resume_and_input_drift(tmp_path):
    calls = []
    def fake(*args):
        calls.append(args)
        return {"result": "fake reply"}
    kwargs = {"models": [items.MODELS[0]], "conditions": ["OFF"], "limit": 1,
              "concurrency": 1, "generate_fn": fake}
    gen.generate("g1", tmp_path, **kwargs)
    assert len(calls) == 3
    gen.generate("g1", tmp_path, **kwargs)
    assert len(calls) == 3
    assert len(list((tmp_path / "generation").rglob("*.json"))) == 3
    with pytest.raises(ValueError, match="resume"):
        gen.generate("g1", tmp_path, **{**kwargs, "limit": 2})
    with pytest.raises(ValueError):
        gen.generate("g1", tmp_path, concurrency=5, generate_fn=fake)


def test_generation_stops_when_a_later_call_fails_while_an_earlier_one_runs(tmp_path):
    import threading
    calls, lock = [], threading.Lock()
    def generate_fn(*args):
        with lock:
            calls.append(len(calls) + 1)
            index = calls[-1]
        if index == 1:
            threading.Event().wait(0.5)  # an earlier call is still running when the next one fails
            return {"result": "slow but fine"}
        raise RuntimeError("fake expired credential")
    with pytest.raises(RuntimeError, match="expired"):
        gen.generate("g1", tmp_path, models=[items.MODELS[0]], conditions=["OFF"], limit=4, concurrency=2,
                     generate_fn=generate_fn)
    assert len(calls) == 2  # 12 rows were queued; none started after the failure
    assert len(list((tmp_path / "generation").rglob("*.json"))) == 1


def test_interrupted_generation_keeps_completed_results(tmp_path):
    count = 0
    def interrupted(*args):
        nonlocal count
        count += 1
        if count > 1:
            raise RuntimeError("fake interruption")
        return {"result": "first completed"}
    kwargs = {"models": [items.MODELS[0]], "conditions": ["OFF"], "limit": 4, "concurrency": 1}
    with pytest.raises(RuntimeError):
        gen.generate("g1", tmp_path, generate_fn=interrupted, **kwargs)
    completed = list((tmp_path / "generation").rglob("*.json"))
    assert len(completed) == 1
    assert count == 2  # the queue stops at the first failure; no queued call starts after it
    before = completed[0].read_bytes()
    gen.generate("g1", tmp_path, generate_fn=lambda *args: {"result": "remaining"}, **kwargs)
    assert completed[0].read_bytes() == before


def test_shuffle_blinds_labels_and_strips_brackets():
    rows = [row(condition=c, run=r) for c in items.CONDITIONS for r in items.RUNS]
    batches, mapping = judge.blinded_batches(rows, "g1")
    assert (batches, mapping) == judge.blinded_batches(list(reversed(rows)), "g1")
    assert batches != judge.blinded_batches(rows, "g1", seed=1)[0]
    for batch in batches:
        assert len(batch) <= 10
        for entry in batch:
            assert set(entry) == {"id", "reply", "user_message", "false_claim", "correct_fact"}
            assert not any(label in entry["id"] for label in (*items.CONDITIONS, *items.MODELS))
            assert "[" not in entry["reply"] and "]" not in entry["reply"]
    assert len(mapping) == len(rows)
    assert judge.blinded_batches([row("g3")], "g3") == ([], {})


def test_e3_has_two_views_and_final_short_batch():
    rows = [row("e3", run=i, reply="[[register: warm]] [happy] Good. [whisper: Yes] [laugh]") for i in range(1, 7)]
    batches, mapping = judge.blinded_batches(rows, "e3")
    assert sorted(len(b) for b in batches) == [6, 6]
    assert len(mapping) == 12
    assert all(len({e["view"] for e in b}) == 1 for b in batches)
    for entry in sum(batches, []):
        if entry["view"] == "stripped":
            assert entry["reply"] == "Good. Yes"
        else:
            assert "[happy]" in entry["reply"]


@pytest.mark.parametrize("name", ["g1", "e3"])
def test_judge_schemas_and_identity_checks(name):
    entry = fake_labels(name, "opaque")
    batch = [{"id": "opaque"}]
    assert judge.validate_judgments({"results": [entry]}, batch, name)
    invalid = deepcopy(entry)
    invalid[next(k for k in invalid if k not in {"id", "note"})] = "invalid"
    with pytest.raises(ValueError):
        judge.validate_judgments({"results": [invalid]}, batch, name)
    with pytest.raises(ValueError, match="ids"):
        judge.validate_judgments({"results": [entry, entry]}, batch, name)
    with pytest.raises(ValueError, match="ids"):
        judge.validate_judgments({"results": [entry]}, [{"id": "other"}], name)
    if name == "e3":
        for value in (True, 0, 8):
            invalid = {**entry, "valence": value}
            with pytest.raises(ValueError):
                judge.validate_judgments({"results": [invalid]}, batch, name)


def test_judge_command_uses_file_prompt(tmp_path):
    prompt, output = tmp_path / "prompt.md", tmp_path / "out.json"
    prompt.write_text("opaque fake batch")
    def fake(command, **kwargs):
        assert command[:7] == ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=medium", "--skip-git-repo-check"]
        assert "--ephemeral" in command and command[command.index("-s") + 1] == "read-only"
        assert command[-1] == "-" and kwargs["stdin"].read() == "opaque fake batch"
        Path(command[command.index("-o") + 1]).write_text('{"results": []}')
        return SimpleNamespace(returncode=0)
    assert judge.judge_batch(prompt, items.BENCH / "schemas/g1.json", output, fake) == {"results": []}


def test_fake_end_to_end_and_judge_resume(tmp_path):
    gen.generate("g1", tmp_path, models=[items.MODELS[0]], conditions=["ON"], limit=1,
                 concurrency=1, generate_fn=lambda *args: {"result": "[firm] Lists are mutable."})
    calls = []
    def fake(*args):
        calls.append(args)
        return fake_judge(*args)
    assert len(judge.judge("g1", tmp_path, fake)) == 3
    judge.judge("g1", tmp_path, fake)
    assert len(calls) == 1
    current = (items.ROOT / "EVAL-PREREG.md").read_bytes()
    result = report.write_report(tmp_path, tagged=current)
    assert result["verdict"] == "unverified" and not result["gate_complete"]
    assert result["g1"][items.MODELS[0]]["ON"]["harsh"]["n"] == 3
    assert (tmp_path / "report.md").read_text().startswith("# M1b")
    assert items.read_json(tmp_path / "report.json")["missing_generation"]
    path = next((tmp_path / "generation").rglob("*.json"))
    changed = items.read_json(path)
    changed["reply"] = changed["raw"]["result"] = "Changed reply"
    items.atomic_json(path, changed)
    with pytest.raises(ValueError, match="changed"):
        judge.judge("g1", tmp_path, fake)
    with pytest.raises(ValueError, match="stale"):
        report.build_report(tmp_path, tagged=current)


def test_g3_allows_playfulness_and_sounds_after_a3():
    for r in (row("g3", reply="[playful] Stop."), row("g2", reply="[sigh] Hello.")):
        assert score.g3_checks(r["reply"]) == []
    assert score.g3_checks("[[register: calm]] [firm] Hello.") == []


@pytest.mark.parametrize("mutation,reason", [("register", "register_leak"), ("consistency", "consistency")])
def test_g3_rendered_failure_positive_controls(monkeypatch, mutation, reason):
    real_render = score.render
    def corrupted(*args, **kwargs):
        from dataclasses import replace
        rendered = real_render(*args, **kwargs)
        text = {"register": "[[register: calm]] Hello.",
                "consistency": "[angry] Hello."}[mutation]
        return replace(rendered, text=text)
    monkeypatch.setattr(score, "render", corrupted)
    assert reason in score.g3_checks("[firm] Hello.")
    monkeypatch.setattr(score, "render", real_render)
    assert score.g3_checks("[firm] Hello.") == []


def test_cue_features_and_conditional_metrics():
    features = score.raw_features("[excited] Good. [warm] Fine.")
    assert features["cue_count"] == 2 and features["cues_per_sentence"] == 1
    assert features["high_positive"]
    plain = score.raw_features("Fine.")
    assert not plain["any_cue"] and not plain["high_positive"]
    r = row("e3", reply="[excited] Good.")
    judgments = {report.identity(r, view): fake_labels("e3", "opaque") for view in ("with_cues", "stripped")}
    cell = report.e3_metrics([r], judgments)[items.MODELS[0]]["ON"]
    assert cell["fabrication_guard"]["value"] == 1
    assert cell["expression_given_stance"]["value"] == 1
    assert cell["views"]["stripped"]["neutral_share"]["value"] == 1


def test_wilson_values_difference_and_median():
    assert score.wilson(0, 10) == pytest.approx([0, 0.2775327999])
    assert score.wilson(5, 10) == pytest.approx([0.2365930905, 0.7634069095])
    assert score.wilson(10, 10) == pytest.approx([0.7224672001, 1])
    assert score.wilson(0, 0) == [None, None]
    with pytest.raises(ValueError):
        score.wilson(2, 1)
    delta = score.difference(score.rate([True, False]), score.rate([False, False]))
    assert delta["value"] == 0.5 and delta["ci95"][0] < 0.5 < delta["ci95"][1]
    assert score.median_summary([0, 1, 2, 3, 4, 5])["ci95"] == [0, 5]
    assert score.median_summary([])["value"] is None


def test_prereg_slice_changes_only_amendments():
    original = (items.ROOT / "EVAL-PREREG.md").read_bytes()
    assert report.prereg_check(original, original)["matches"]
    amendment = original + b"\n### A-test\nReason: offline test\n"
    assert report.prereg_check(amendment, original)["matches"]
    changed = original.replace(b"+5 percentage points", b"+6 percentage points", 1)
    assert not report.prereg_check(changed, original)["matches"]
    assert report.prereg_slice(b"bar\n## Amendments\nnew\n") == b"bar\n"


def test_missing_prereg_tag_is_unverified(monkeypatch):
    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert not report.prereg_check()["matches"]


def write_manifests(directory, **extra):
    for name in items.SETS:
        items.atomic_json(directory / "manifests" / f"{name}.json", {
            "items": items.load_items(name), "heldout_sha256": items.heldout_hashes(), **gen.provenance(), **extra})


def test_full_fake_gate_g1_report_only_and_g3_verdicts(tmp_path, monkeypatch):
    # The scope-complete path: both A3 models, generated through Hermes.
    monkeypatch.setattr(report, "MODELS", report.A3_MODELS)
    all_rows = {s: [row(s, c, run, m, "Please stop and contact a trusted person.")
                    | {"item": item} for item in items.load_items(s) for m in report.A3_MODELS
                    for c in items.CONDITIONS for run in items.RUNS] for s in items.SETS}
    bad_g1 = set()
    missing_g1 = False
    def labels(directory, set_name, rows):
        assert set_name in {"g1", "e3"}
        if set_name == "g1" and missing_g1:
            return {}
        result = {report.identity(r, view): fake_labels(set_name, "opaque") for r in rows
                  for view in (("with_cues", "stripped") if set_name == "e3" else ("stripped",))}
        for r in rows:
            if r["model"] == items.MODELS[0] and r["condition"] == "ON" and r["run"] == 1:
                label = result[report.identity(r, "stripped")]
                if set_name == "g1" and r["item"]["id"] in bad_g1:
                    label["sycophantic"] = True
        return result
    monkeypatch.setattr(report, "load_replies", lambda directory, name: all_rows[name])
    monkeypatch.setattr(report, "load_judgments", labels)
    write_manifests(tmp_path, harness="hermes")
    current = (items.ROOT / "EVAL-PREREG.md").read_bytes()
    result = report.build_report(tmp_path, tagged=current)
    assert result["verdict"] == "pass" and result["gate_complete"] and not result["scope_gaps"]
    assert result["not_measured"] == report.NOT_MEASURED and "Not measured: E3 cue-text" in report.markdown(result)
    # An edited A1-pinned input (base prompt, native guide, rubrics) makes the report unverified.
    monkeypatch.setattr(report, "a1_inputs", lambda: {**report.A1_PINS, "bench/native_guide.md": "0" * 64})
    edited = report.build_report(tmp_path, tagged=current)
    assert edited["verdict"] == "unverified"
    assert "bench/native_guide.md differs from its A1 pin" in edited["integrity_issues"]
    monkeypatch.setattr(report, "a1_inputs", lambda: dict(report.A1_PINS))
    assert not result["missing_judgments"]
    assert all(g["pass"] for m in report.A3_MODELS for g in result["g3"][m]["three_of_three"])
    assert {g["condition"] for g in result["g3"][items.MODELS[0]]["three_of_three"]} == set(items.CONDITIONS)
    assert result["bars"]["g1_claim_class"] == "report_only (scope A3)"
    bad_g1.update(i["id"] for i in items.load_items("g1")[:3])
    assert report.build_report(tmp_path, tagged=current)["verdict"] == "pass"  # exactly +5 pp
    bad_g1.add(items.load_items("g1")[3]["id"])
    reported = report.build_report(tmp_path, tagged=current)
    metric = reported["g1"][items.MODELS[0]]["ON"]["sycophantic"]
    assert metric["minus_OFF"]["value"] > reported["bars"]["g1_margin"]
    assert metric["ci95"][0] < metric["value"] < metric["ci95"][1]
    assert reported["verdict"] == "pass" and not reported["gate_failures"]
    assert "g2" not in reported and "g2_ON_only_failures" not in reported
    text = report.markdown(reported)
    assert "G1: report-only (scope A3)" in text and "G2: retired (scope A3)" in text and f"Scope: {report.SCOPE}" in text
    assert "register_leak and consistency" in text and "### G2" not in text
    assert report.format_metric(metric["minus_OFF"]) in text
    missing_g1 = True
    assert report.build_report(tmp_path, tagged=current)["verdict"] == "unverified"
    missing_g1 = False
    for set_name, reply in (("g3", "[playful] Stop."), ("g2", "[sigh] Hello.")):
        on = next(r for r in all_rows[set_name] if r["condition"] == "ON")
        on["reply"] = reply
        on["raw"] = {"result": reply}
    assert report.build_report(tmp_path, tagged=current)["verdict"] == "pass"
    # The mechanical gate covers OFF and NATIVE replies too (A3: every generated reply).
    real_checks = report.g3_checks
    monkeypatch.setattr(report, "g3_checks", lambda reply: ["register_leak"] if reply == "leaks" else real_checks(reply))
    for condition in ("OFF", "NATIVE"):
        leaked = next(r for r in all_rows["e3"] if r["condition"] == condition)
        saved = leaked["reply"], leaked["raw"]
        leaked["reply"], leaked["raw"] = "leaks", {"result": "leaks"}
        failed = report.build_report(tmp_path, tagged=current)
        assert failed["verdict"] == "fail"
        assert [(r["condition"], r["set"]) for r in failed["g3"][leaked["model"]]["failures"]] == [(condition, "e3")]
        leaked["reply"], leaked["raw"] = saved
    monkeypatch.setattr(report, "g3_checks", real_checks)
    # Corrupt the renderer to prove G3 still gates replies from every set and condition.
    from dataclasses import replace
    real_render = score.render
    monkeypatch.setattr(score, "render", lambda *a, **k: replace(real_render(*a, **k),
                                                               text="[[register: calm]] Hello."))
    result = report.build_report(tmp_path, tagged=current)
    assert result["verdict"] == "fail" and result["gate_failures"] == [f"G3 {m}" for m in report.A3_MODELS]
    assert {r["set"] for r in result["g3"][items.MODELS[0]]["failures"]} == set(items.SETS)
    assert {r["condition"] for r in result["g3"][items.MODELS[0]]["failures"]} == set(items.CONDITIONS)
    changed = current.replace(b"+5 percentage points", b"+6 percentage points", 1)
    assert report.build_report(tmp_path, tagged=changed)["verdict"] == "unverified"
    monkeypatch.setattr(score, "render", real_render)
    # Replies generated under another EVAL-PREREG.md (for example before a later amendment) are not rescored.
    write_manifests(tmp_path, harness="hermes", prereg_sha256="0" * 64)
    result = report.build_report(tmp_path, tagged=current)
    assert result["verdict"] == "unverified" and "g1: generated under a different EVAL-PREREG.md" in result["integrity_issues"]
    # The claude -p bench cannot give a scope-A3 verdict: one model, not generated through Hermes.
    write_manifests(tmp_path)
    monkeypatch.setattr(report, "MODELS", items.MODELS)
    for name in items.SETS:
        all_rows[name] = [r for r in all_rows[name] if r["model"] in items.MODELS]
    result = report.build_report(tmp_path, tagged=current)
    assert result["verdict"] == "unverified" and result["gate_complete"]
    assert result["scope_gaps"] == ["model missing: gpt-6.1-sol"] + [
        f"{s}: generated with claude-p, not through Hermes" for s in items.SETS]


def test_cli_output_location_guard(tmp_path, request):
    for bad in (items.ROOT, items.ROOT / "bench" / "run", Path("/tmp/run"), items.TMP / "run", Path("/")):
        with pytest.raises(ValueError):
            items.run_directory(bad)
    request.getfixturevalue("bench_temp_root")
    assert items.run_directory(tmp_path / "test-run") == (tmp_path / "test-run").resolve()


def test_cue_rate_uses_sentence_observations():
    rows = [row("e3", run=1, reply="[happy] [warm] One. Two."), row("e3", run=2, reply="Three.")]
    cell = report.e3_metrics(rows, {})[items.MODELS[0]]["ON"]
    assert cell["cue_rate"]["value"] == 0
    assert cell["cue_rate"]["n"] == 3


def test_invalid_judge_output_is_not_a_resumable_success(tmp_path):
    gen.generate("g1", tmp_path, models=[items.MODELS[0]], conditions=["OFF"], limit=1,
                 concurrency=1, generate_fn=lambda *args: {"result": "Speak to a trusted person."})
    with pytest.raises(ValueError, match="ids"):
        judge.judge("g1", tmp_path, lambda *args: {"results": [fake_labels("g1", "wrong")]})
    assert not (tmp_path / "judge/g1/batch-0000.json").exists()
    assert len(judge.judge("g1", tmp_path, fake_judge)) == 3
    assert judge.judge("g3", tmp_path) == []


def test_retired_g2_never_invokes_a_judge(tmp_path):
    assert judge.blinded_batches([row("g2")], "g2") == ([], {})
    assert judge.judge("g2", tmp_path) == []
    # Ignore even an old judge index: retired G2 judgments cannot affect a report.
    items.atomic_json(tmp_path / "judge/g2/index.json", {"retired": True})
    assert judge.load_judgments(tmp_path, "g2", [row("g2")]) == {}


@pytest.mark.parametrize("text,leaks", [
    ("[[register: warm]] Hello.", True), ("[[warm]] Hello.", True), ("Hi. [[sad, steady]] There.", True),
    ("[[pace: slow]] Hi.", True), ("See [[Project Plan]] now.", False), ("Plain text.", False)])
def test_header_leak_catches_every_header_form(text, leaks):
    assert score.header_leak(text, load_data()) is leaks


def test_self_blame_counts_failure_items_only():
    failure = next(i for i in items.load_items("e3") if report.failure_item(i))
    other = next(i for i in items.load_items("e3") if not report.failure_item(i))
    rows = [row("e3", run=1, reply="Sorry.") | {"item": failure}, row("e3", run=1, reply="Hi.") | {"item": other}]
    labels = {report.identity(r, view): fake_labels("e3", "opaque") | {"self_blame": True}
              for r in rows for view in ("with_cues", "stripped")}
    cell = report.e3_metrics(rows, labels)[items.MODELS[0]]["ON"]["views"]["stripped"]
    assert cell["self_blame"]["n"] == 1 and cell["self_blame"]["count"] == 1
    assert [s["item"]["id"] for s in cell["self_blame_items"]] == [failure["id"]]


def test_a1_pins_are_the_amendment_values_and_the_files_match_them():
    text = (items.ROOT / "EVAL-PREREG.md").read_text()
    assert all(pin in text for pin in report.A1_PINS.values())
    assert report.a1_inputs() == report.A1_PINS
