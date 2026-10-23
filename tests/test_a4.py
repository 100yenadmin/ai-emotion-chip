"""A4 offline contracts: no installed Hermes, relay, credentials, or model processes."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import random
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest

from bench import gen, items, judge, report, score
from bench.hermes import run, text
from bench.hermes.bench_probe import probe


def driver():
    spec = importlib.util.spec_from_file_location("a4_test_turn", run.HERE / "turn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalization_rebuild_prefix():
    assert probe.normalise("S\nToday's date (as of the last context rebuild): date\nSession ID: a\nModel: m") == "S\nModel: m"


def relay_fixture():
    def prepare(messages, names):
        return messages[0]["content"], [{"message": {"role": m["role"], "content": [
            {"type": "text", "text": m["content"]}]}} for m in messages[1:]]
    admission = SimpleNamespace(restore_queried_turn=lambda p, q: p,
        stable_request_context=lambda p, q, session=False: p, pin_message_breakpoint=lambda p, q: p,
        stream_tool_input=lambda p: p)
    expected = probe.expected_relay("S", [{"role": "user", "content": "item"}], {"skill_view": "skill_view"},
        prepare_history=prepare, admission=admission, reminder="R1 exact")
    record = {"blocks": [{"kind": "S1", "placement": "system-prefix", "text": "Identity."},
        {"kind": "S2", "placement": "system-prefix", "text": "billing=value"},
        {"kind": "M1", "placement": "newest-user", "text": "test@example.invalid"}]}
    final = {"system": [{"type": "text", "text": "Identity."}, {"type": "text", "text": "billing=value"},
                        {"type": "text", "text": "S"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "item"},
            {"type": "text", "text": "test@example.invalid"}, {"type": "text", "text": "R1 exact"}]}]}
    return expected, record, final


@pytest.mark.parametrize("mutation", ["none", "append", "system", "unknown_m1", "r1", "role", "earlier"])
def test_relay_request_equality(mutation):
    expected, record, final = relay_fixture()
    if mutation == "append":
        final["messages"][0]["content"][0]["text"] += " hidden instruction"
    elif mutation == "system":
        final["system"].insert(1, {"type": "text", "text": "unknown"})
    elif mutation == "unknown_m1":
        final["messages"][0]["content"][1]["text"] = "Use more effort."
    elif mutation == "r1":
        final["messages"][0]["content"][-1]["text"] += " changed"
    elif mutation == "role":
        final["messages"][0]["role"] = "assistant"
    elif mutation == "earlier":
        expected["messages"].insert(0, {"role": "assistant", "content": [{"type": "text", "text": "previous"}]})
        final["messages"].insert(0, {"role": "assistant", "content": [{"type": "text", "text": "previous plus"}]})
    final["system"][-1]["cache_control"] = {"type": "ephemeral"}
    assert bool(probe.compare_request(expected, final, record)) is (mutation != "none")


def test_codex_fixed_request_fixture():
    record = json.loads((Path(__file__).parent / "fixtures/a4-codex-request.json").read_text())
    fixture = record["final_content"]
    def build(**kwargs):
        assert kwargs == {**record["options"], "messages": record["messages"]}
        return deepcopy(record["built_content"])
    def preflight(value, **kwargs):
        assert kwargs == {"sanitize_harmony_tokens": True}
        assert value == record["built_content"]
        return deepcopy(record["final_content"])
    expected = probe.expected_codex(record["messages"][0]["content"], record["messages"][1:], record["options"],
                                   build_kwargs=build, preflight=preflight)
    assert expected == fixture
    assert probe.compare_request(expected, {**fixture, "stream": True, "reasoning": {"effort": "medium"}}, {}) == []
    assert probe.compare_request(expected, {**fixture, "input": []}, {}) == ["input"]


def test_capture_receipts_have_schemas_and_no_text():
    final = {"instructions": "S", "input": [{"role": "user", "content": "hello"}],
             "tools": [{"name": "skill_view", "parameters": {"type": "object"}}]}
    row = probe.capture_row("S", final["input"], final, final, {})
    assert row["messages"] == [probe.component(final["input"][0])]
    assert row["tool_schemas"] == {"skill_view": probe.component(final["tools"][0])["sha256"]}
    assert "hello" not in json.dumps(row) and not row["checks"]


def test_relay_wraps_final_stage_and_fails_closed(tmp_path, monkeypatch):
    turn = driver()
    with pytest.raises(RuntimeError, match="no relay"):
        turn.wrap_upstream(tmp_path)
    module = ModuleType("fake.admission")
    module.Handler = object
    module.pin_message_breakpoint = lambda payload: payload
    module.stream_tool_input = lambda payload: json.dumps({**json.loads(payload), "system": "after streaming"}).encode()
    monkeypatch.setitem(sys.modules, "fake.admission", module)
    captured = []
    assert turn.wrap_upstream(tmp_path, captured.append) == ["fake.admission"]
    payload = b'{"system":"before"}'
    assert json.loads(module.stream_tool_input(payload))["system"] == captured[0]["system"] == "after streaming"


def test_capture_signature_and_last_codex_preflight(tmp_path, monkeypatch):
    turn = driver()
    loop, package, codex = ModuleType("agent.conversation_loop"), ModuleType("agent"), ModuleType("agent.transports.codex")
    def build(agent, *, api_messages, api_call_count, messages, tools_for_api):
        return "original result"
    loop.build_api_request = build
    package.conversation_loop = loop
    class Transport:
        def preflight_kwargs(self, value, **kwargs):
            return value
    codex.ResponsesApiTransport = Transport
    for name, module in (("agent", package), ("agent.conversation_loop", loop), ("agent.transports.codex", codex)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(turn, "expected_codex", lambda S, M, options: {"instructions": S, "input": M})
    agent = SimpleNamespace(provider="openai-codex", model="gpt-6.1-sol", _codex_reasoning_replay_enabled=True)
    send_runtime(monkeypatch, loop, agent)
    read_runtime(monkeypatch)
    agent._current_task_id = "synthetic"
    agent._tool_result_content_for_active_model = lambda name, result: result
    finals, installed = turn.install_text_capture({"item_dir": str(tmp_path), "prompt": "hello"}, agent)
    assert inspect.signature(loop.build_api_request) == inspect.signature(build)
    assert loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}],
        api_call_count=1, messages=[], tools_for_api=[]) == "original result"
    assert installed == ["ResponsesApiTransport.preflight_kwargs"]
    value = {"model": agent.model, "instructions": "S", "input": [{"role": "user", "content": "hello"}]}
    assert Transport().preflight_kwargs(value) is value
    Transport().preflight_kwargs({**value, "input": []})
    assert len(finals) == 1 and finals[1]["checks"] == ["input"]
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "tool_calls": [
        {"id": "call", "call_id": "call", "response_item_id": "fc_call", "function": {
            "name": "skill_view", "arguments": '{ "name": "emotion-chip-voice" }'}}]},
        {"role": "tool", "tool_call_id": "call", "content": read_runtime(monkeypatch)["skill_view"]}]
    for corrupted in (False, True):
        messages = deepcopy(history)
        if corrupted:
            messages[0]["content"] += " injected later"
        api = deepcopy(messages)
        loop._canonicalize_api_tool_calls(api)
        loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + api,
            api_call_count=2, messages=messages, tools_for_api=[])
        Transport().preflight_kwargs({**value, "input": api})
        assert finals[2]["history_matches"] is (not corrupted)
    history[-1]["content"] = "[hermes note: stub]"
    loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + history,
        api_call_count=2, messages=history, tools_for_api=[])
    Transport().preflight_kwargs({**value, "input": history})
    assert finals[2]["history_matches"] is False


def test_text_configs_and_published_sections(tmp_path):
    assert items.MODELS == ("claude-opus-5-5", "gpt-6.1-sol")
    assert gen.CLAUDE_P_MODELS == ("claude-opus-5-5",)
    with pytest.raises(ValueError, match="unsupported"):
        gen.generate("g1", tmp_path, models=["gpt-6.1-sol"], generate_fn=lambda *a: {"result": "fake"})
    for model in items.MODELS:
        for arm in items.CONDITIONS:
            cfg = text.hermes_config(model, arm, tmp_path)
            assert cfg["tts"] == {"provider": "emotion-chip", "model": "eleven_v4"}
            assert cfg["auth"]["adopt_external_logins"] is False and not cfg["fallback_providers"]
            assert cfg["agent"]["reasoning_effort"] == "medium" and "openai_runtime" not in cfg["model"]
            assert "fish-audio" not in cfg["plugins"]["enabled"]
            assert "emotion-chip" in text.sections(model, "ON")
            if model == items.MODELS[0]:
                assert cfg["plugins"]["entries"][run.PROVIDER]["settings"]["session_mode"] is False
            else:
                assert cfg["model"] == {"provider": "openai-codex", "default": model}
    native = text.sections(items.MODELS[0], "NATIVE")
    assert native["bench-harness"] == text.HARNESS
    assert native["bench-native-1"] + "\n\n" + native["bench-native-2"] == (items.BENCH / "native_guide.md").read_text().strip()


def test_seeded_order_matches_a4():
    rng = random.Random(20261023)
    units = [(s, i["id"], r) for s in items.SETS for i in items.load_items(s) for r in items.RUNS]
    rng.shuffle(units)
    expected = []
    for s, i, r in units:
        arms = list(items.CONDITIONS)
        rng.shuffle(arms)
        expected += [(s, i, r, c) for c in arms]
    actual = text.cell_order()
    assert [(r["set"], r["item"]["id"], r["run"], r["condition"]) for r in actual] == expected
    assert len(actual) == 684 and len(text.cell_order(True)) == 24


def phase_rows(n=5):
    return [{"set": "g1", "item": {"id": str(i)}, "run": 1, "condition": "OFF", "model": items.MODELS[0]}
            for i in range(n)]


def attempt(tokens=100, bad=()):
    return {"checks": {k: k not in bad for k in (1, 2, 3, 5)}, "tokens": tokens, "reply": "reply"}


def test_immediate_rerun_once_keeps_slot():
    seen, receipts = [], []
    def execute(row, number):
        seen.append((row["item"]["id"], number))
        return attempt(bad=(5,) if row["item"]["id"] == "0" and number == 1 else ())
    result = text.run_phase(phase_rows(2), execute, receipts.append)
    assert seen == [("0", 1), ("0", 2), ("1", 1)]
    assert len(receipts) == 3 and [v["admitted"] for v in receipts] == [False, True, True]
    assert not result["stopped"]


def test_deferred_token_pass_original_order_same_median_and_outlier():
    seen, receipts = [], []
    def execute(row, number):
        i = int(row["item"]["id"])
        seen.append((i, number))
        return attempt(200 if i in {0, 2} and (number == 1 or i == 2) else 100)
    result = text.run_phase(phase_rows(), execute, receipts.append)
    assert seen == [(i, 1) for i in range(5)] + [(0, 2), (2, 2)]
    assert result["medians"]["OFF"] == 100
    assert [v["median"] for v in receipts] == [100] * 7
    assert receipts[-2]["admitted"] and receipts[-1]["outlier"] and not receipts[-1]["admitted"]


@pytest.mark.parametrize("check", [1, 2, 3])
def test_critical_repeat_stops_model(check):
    seen = []
    def execute(row, number):
        seen.append(number)
        return attempt(bad=(check,))
    result = text.run_phase(phase_rows(), execute, lambda v: None)
    assert seen == [1, 2] and result["stopped"] and result["critical_failures"] == 2


@pytest.mark.parametrize("check", [1, 2, 3, 5])
def test_validity_checks_fail_closed(check):
    capture = {"api_call_count": 1, "system": {"sha256": "gold"}, "checks": [], "canaries": {"marker-1": False},
               "history_matches": True, "model": items.MODELS[0]}
    result = {"skill_tree": "tree", "api_calls": 1, "captures": [capture], "completed": True, "final_response": "reply"}
    memory = True
    if check == 1:
        capture["canaries"]["marker-1"] = True
    elif check == 2:
        capture["system"] = {"sha256": "other"}
    elif check == 3:
        capture["history_matches"] = False
    else:
        memory = False
    golden = {"skill_tree": "tree", "system_sha256": "gold", "route": {"model": items.MODELS[0]}}
    checks = text.validity(result, golden, [], memory)
    assert checks[check] is False and sum(checks.values()) == 3
    assert not any(text.validity({}, golden, [], False).values())
    capture["history_matches"] = True
    result["skill_tree"] = "changed-tree"
    assert text.validity(result, golden, [], memory)[3] is False


def test_attempt_storage_excludes_invalid_and_pilot_replies(tmp_path):
    row = {**phase_rows(1)[0], "split": "heldout"}
    for phase, admitted, number in (("full", False, 1), ("pilot", True, 1), ("full", True, 2)):
        value = {**attempt(), "row": row, "admitted": admitted, "attempt": number}
        text.publish_attempt(tmp_path, phase, value)
        assert len(judge.load_replies(tmp_path, "g1")) == int(phase == "full" and admitted)
    assert len(list((tmp_path / "attempts").rglob("*.json"))) == 3
    assert len(run.read_jsonl(gen.failures_path(tmp_path, "g1"))) == 1


def test_parent_refresh_and_isolated_home(tmp_path):
    pair = {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_at": 100}
    refreshed = []
    def refresh(old):
        refreshed.append(old)
        return {**old, "expires_at": 2000}
    manager = text.TokenPair(lambda: pair, refresh, clock=lambda: 0)
    home = tmp_path / "home"
    home.mkdir()
    manager.provision(home)
    assert refreshed == [pair]
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"] == {
        "access_token": "fake-access", "refresh_token": "harness-owned-no-refresh"}
    with pytest.raises(ValueError, match="isolated"):
        manager.provision(tmp_path / "profiles" / "home")


def fake_login(path, exp):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"providers": {"openai-codex": {"tokens": {
        "access_token": f"head.{claims}.sig", "refresh_token": "owner-refresh"}}}}))
    return path


def test_borrowed_login_is_read_only_and_never_refreshed(tmp_path):
    source = fake_login(tmp_path / "auth.json", 4000)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    load, refresh = text.borrowed_pair(source)
    assert load()["expires_at"] == 4000
    home = tmp_path / "home"
    home.mkdir()
    text.TokenPair(load, refresh, clock=lambda: 0).provision(home)
    assert json.loads((home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"] == {
        "access_token": load()["access_token"], "refresh_token": "harness-owned-no-refresh"}
    late = tmp_path / "late"
    late.mkdir()
    with pytest.raises(RuntimeError, match="^STOP: credential or sign-in need$"):
        text.TokenPair(load, refresh, clock=lambda: 3500).provision(late)
    assert not (late / "auth.json").exists()
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_main_borrows_a_named_login(tmp_path, monkeypatch, bench_temp_root):
    argv = fake_main_runtime(tmp_path, monkeypatch)
    source = fake_login(tmp_path / "auth.json", 4000)
    monkeypatch.setattr(text, "load_paths", lambda path: {
        "host": tmp_path / "host", "harness_auth_home": tmp_path / "parent", "codex_auth_source": source})
    seen = []
    def run_model(self, model, route_record):
        seen.append(self.tokens.load()["expires_at"])
        with pytest.raises(RuntimeError, match="STOP"):
            self.tokens.refresh({})
        return {}
    monkeypatch.setattr(text.Harness, "run_model", run_model)
    text.main(argv)
    assert seen == [4000] * len(items.MODELS)


def test_judge_model_pools_and_claude_environment(tmp_path):
    rows = [{"set": "g1", "split": "heldout", "item": items.load_items("g1")[0], "model": m,
             "condition": c, "run": r, "reply": "reply"}
            for m in items.MODELS for c in items.CONDITIONS for r in items.RUNS]
    batches, mapping = judge.blinded_batches(rows, "g1")
    assert len(batches) == 2
    assert all(len({mapping[e["id"]]["identity"][2] for e in b}) == 1 for b in batches)
    assert judge.batch_judges(batches, mapping) == ["gpt-6.1-sol", "claude-opus-5-5"]
    prompt = tmp_path / "prompt.md"
    prompt.write_text("rubric and blind batch")
    def fake(command, **kwargs):
        assert command == ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "medium",
                           "--tools", "", "--max-turns", "1", "--output-format", "json"]
        assert kwargs["cwd"] == kwargs["env"]["CLAUDE_CONFIG_DIR"]
        assert kwargs["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
        assert kwargs["input"] == prompt.read_text()
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": '{"results": []}'}))
    assert judge.claude_judge_batch(prompt, None, None, fake, lambda: "fake") == {"results": []}


def test_public_a4b_placeholders():
    record = {"model": {"blocks": [{"kind": "S2", "text": "billing: field=private-value; other=123", "placement": "system-prefix"},
                                  {"kind": "M1", "text": "test@example.invalid", "placement": "newest-user"}]}}
    public = text.public_a4b(record)
    assert public["model"]["blocks"][0]["text"] == "billing: field=<billing-value>; other=<billing-value>"
    assert public["model"]["blocks"][1]["text"] == "<account-email>"


def test_public_a4b_working_directory():
    instance = json.loads((Path(__file__).parent / "fixtures/a5-e1-block.json").read_text())
    real = instance["instance"].replace("/bench-runs/run-1", "/Users/synthetic/scratch/run-1")
    record = {"model": {"blocks": [{"kind": "E1", "placement": "trailing-system", "text": real},
                                  {"kind": "S2", "placement": "system-prefix",
                                   "text": "x: cc=1; Primary working directory: /synthetic/cwd"}]}}
    public = text.public_a4b(record)["model"]["blocks"]
    assert public[0]["text"] == real.replace(real.split("Primary working directory: ")[1].split("\n")[0], "<cwd>")
    assert " - Primary working directory: <cwd>\n - Is a git repository: false" in public[0]["text"]
    assert "/Users/synthetic" not in json.dumps(public)
    assert public[1]["text"] == "x: cc=<billing-value>; Primary working directory: <cwd>"


def test_check_two_records_request_differences_without_failing():
    capture = {"api_call_count": 1, "system": {"sha256": "gold"}, "checks": ["messages"],
               "canaries": {"marker-1": False}, "history_matches": True, "model": items.MODELS[0]}
    result = {"skill_tree": "tree", "api_calls": 1, "captures": [capture], "completed": True, "final_response": "reply"}
    golden = {"skill_tree": "tree", "system_sha256": "gold", "route": {"model": items.MODELS[0]}}
    assert text.validity(result, golden, [], True)[2] is True
    assert capture["checks"] == ["messages"]
    capture["model"] = "other"
    assert text.validity(result, golden, [], True)[2] is False


def test_report_honors_backend_dialect_and_missing_breakdown(tmp_path, monkeypatch):
    current = (items.ROOT / "EVAL-PREREG.md").read_bytes()
    row = {"set": "g1", "split": "heldout", "model": items.MODELS[0], "condition": "OFF", "run": 1,
           "item": items.load_items("g1")[0], "reply": "reply", "raw": {"result": "reply"}}
    seen = []
    def gate(reply, **kwargs):
        seen.append(kwargs["dialect"])
        return ["consistency"] if kwargs["dialect"] == "eleven_v4" else []
    monkeypatch.setattr(report, "g3_checks", gate)
    monkeypatch.setattr(report, "load_judgments", lambda *a: {})
    monkeypatch.setattr(report, "load_replies", lambda d, s: [row] if s == "g1" else [])
    for harness, dialect in (("hermes", "eleven_v4"), ("claude-p", "fish_s2")):
        for s in items.SETS:
            items.atomic_json(tmp_path / "manifests" / f"{s}.json", {"items": items.load_items(s),
                "models": list(items.MODELS), "harness": harness, "heldout_sha256": items.heldout_hashes(),
                "route_identities": {"test_digest": "abc"}, **gen.provenance()})
        result = report.build_report(tmp_path, tagged=current)
        assert seen[-1] == dialect
        assert result["missing_judgments_by_cell"]["g1"][items.MODELS[0]]["OFF"] == 1
        assert result["provenance"]["g1"]["route_identities"] == {"test_digest": "abc"}
        if harness == "hermes":
            assert "g1: generation lacks A4 admission" in result["integrity_issues"]
        assert not result["scope_gaps"] if harness == "hermes" else result["scope_gaps"]
        assert "text-only speech proxy" in report.markdown(result)
        metric = next(s for s in report.markdown(result).splitlines() if "Not measured: E3 cue-text" in s)
        assert "recall and false-flag rate" in metric and "value" not in metric


def test_g3_passes_the_selected_dialect(monkeypatch):
    real = score.render
    def render(value, dialect, **kwargs):
        from dataclasses import replace
        result = real(value, dialect, **kwargs)
        return replace(result, text="[[register: calm]] leaked") if dialect == "eleven_v4" else result
    monkeypatch.setattr(score, "render", render)
    assert "register_leak" in score.g3_checks("[firm] Hello", dialect="eleven_v4")
    assert score.g3_checks("[firm] Hello", dialect="fish_s2") == []


def test_missing_call_capture_is_invalid():
    golden = {"system_sha256": "S", "route": {"model": items.MODELS[0]}}
    capture = {"api_call_count": 1, "system": {"sha256": "S"}, "model": items.MODELS[0],
               "checks": [], "canaries": {}, "history_matches": True}
    result = {"captures": [capture], "api_calls": 2, "completed": True, "final_response": "reply"}
    checked = text.validity(result, golden, [], True)
    assert not any(checked[k] for k in (1, 2, 3))


def test_text_phase_sequence_and_owner_route_gate(tmp_path, monkeypatch):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path / "scratch", tmp_path / "out", {}, {}, None, None)
    monkeypatch.setattr(harness, "warm", lambda m: None)
    assert harness.run_model(model) == {"missing": "owner-approved A4b required"}
    harness.records = {model: {"owner_approved": True, "identity": {}}}
    seen, published = [], []
    def execute(phase, row, number):
        seen.append((phase, row))
        return attempt()
    monkeypatch.setattr(harness, "execute", execute)
    monkeypatch.setattr(text, "publish_attempt", lambda out, phase, value: published.append((phase, value)))
    receipts = harness.run_model(model)
    assert [len(receipts[p]["attempts"]) for p in ("reference", "pilot", "full")] == [1, 24, 684]
    assert seen[0][0] == "reference" and seen[0][1]["condition"] == "OFF"
    assert seen[0][1]["item"]["id"] == items.load_items("g1")[0]["id"]
    assert all(v["admitted"] for p, v in published)


def test_streaming_metadata_only_on_tools():
    value = {"tools": [{"name": "skill_view", "eager_input_streaming": True}],
             "messages": [{"role": "user", "content": {"eager_input_streaming": "must remain"}}]}
    plain = probe.content_only(value)
    assert plain["tools"] == [{"name": "skill_view"}]
    assert plain["messages"] == value["messages"]


def test_generated_guidance_uses_hermes_section_limit(tmp_path, monkeypatch):
    upstream, provider = tmp_path / "upstream", tmp_path / "provider"
    for name in ("agent/transports/codex.py", "agent/codex_responses_adapter.py"):
        path = upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fake source\n")
    provider.mkdir()
    (provider / "__init__.py").write_text("")
    command = tmp_path / "fake-cli"
    command.write_text("fake binary; never executed")
    paths = {"host": upstream, "chip_repo": tmp_path, "provider_src": provider,
             "canaries": ["fake-machine-repository"],
             "claude_command": command, "claude_code_version": "2.1.306"}
    monkeypatch.setattr(run, "rev", lambda *a: "fake-commit")
    monkeypatch.setattr(run, "archive", lambda repo, ref, dest: dest.mkdir())
    monkeypatch.setattr(text.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="2.1.306 (Claude Code)"))
    harness = text.Harness(tmp_path / "scratch", tmp_path / "out", paths, {}, None, None)
    harness.prepare()
    assert harness.provenance["claude_binary_sha256"] == hashlib.sha256(command.read_bytes()).hexdigest()
    plugin = harness.run / "src/bench-guidance/__init__.py"
    public = text.sections(items.MODELS[0], "NATIVE")
    plugin.with_name("sections.json").write_text(json.dumps(public))
    namespace = {"__file__": str(plugin)}
    exec(plugin.read_text(), namespace)
    seen = {}
    def register(key, callback, *, max_chars):
        assert 0 < max_chars <= 4000  # Pinned Hermes v0.22.2 maximum.
        seen[key] = callback({})
    namespace["register"](SimpleNamespace(register_system_prompt_section=register))
    assert seen == public


def test_warm_includes_ephemeral_system_prompt(tmp_path, monkeypatch):
    turn = driver()
    module = ModuleType("agent.system_prompt")
    module.build_system_prompt = lambda agent: "S"
    monkeypatch.setitem(sys.modules, "agent.system_prompt", module)
    agent = SimpleNamespace(ephemeral_system_prompt="ephemeral", tools=[], close=lambda: None)
    monkeypatch.setattr(turn, "build_agent", lambda cwd, text=False: (agent, None, {}))
    value = turn.warm({"cwd": str(tmp_path), "item_dir": str(tmp_path)})
    assert value["system_sha256"] == probe.digest("S\n\nephemeral")


def relay_tools_fixture():
    return json.loads((Path(__file__).parent / "fixtures/a4-relay-tools.json").read_text())


@pytest.mark.parametrize("model", items.MODELS)
def test_warm_records_route_sent_tools(tmp_path, monkeypatch, model):
    turn = driver()
    fixture = relay_tools_fixture()
    root = tmp_path / "pinned-relay"
    root.mkdir()
    (root / "__init__.py").write_text('raise AssertionError("registration must not run")\n')
    (root / "fixture.json").write_text(json.dumps(fixture))
    (root / "directsdk.py").write_text(
        'import json\nfrom pathlib import Path\n'
        'fixture = json.loads(Path(__file__).with_name("fixture.json").read_text())\n'
        'def request_body(kwargs):\n'
        '    assert kwargs == fixture["request"]\n'
        '    return json.dumps(fixture["body"]), [], {}\n'
        'SKILLS_REMINDER = "fixture reminder"\n'
        'def prepare_history(messages, names):\n'
        '    return messages[0]["content"], [{"message": {"role": "user", "content": '
        '[{"type": "text", "text": messages[1]["content"]}]}}]\n')
    (root / "admission.py").write_text('"""Inert fixture module."""\n')
    for name in ("a4_pinned_relay", "a4_pinned_relay.directsdk", "a4_pinned_relay.admission"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
        monkeypatch.delitem(sys.modules, name)
    module = ModuleType("agent.system_prompt")
    module.build_system_prompt = lambda agent: "S"
    monkeypatch.setitem(sys.modules, "agent.system_prompt", module)
    agent = SimpleNamespace(ephemeral_system_prompt="", tools=deepcopy(fixture["request"]["tools"]),
        provider="openai-codex" if model == items.MODELS[1] else "relay", close=lambda: None)
    monkeypatch.setattr(turn, "build_agent", lambda cwd, text=False: (agent, None, {}))
    monkeypatch.setattr(turn, "golden_delta", lambda *args: {})
    value = turn.warm({"cwd": str(tmp_path), "item_dir": str(tmp_path), "text": True,
                       "provider_path": str(root)})
    assert value["tools"] == ["skill_manage", "skill_view", "skills_list"]
    assert value["sent_tools"] == (value["tools"] if model == items.MODELS[1] else fixture["sent_tools"])
    assert agent.tools == fixture["request"]["tools"]
    if model == items.MODELS[0]:
        # The expectation uses the same loader and never executes registration.
        expected = probe.expected_relay("S", [{"role": "user", "content": "hello"}],
                                        {name: name for name in value["tools"]}, provider_path=root)
        assert expected["messages"][0]["content"][-1]["text"] == "fixture reminder"
        assert Path(sys.modules["a4_pinned_relay.directsdk"].__file__).parent == root
    else:
        assert "a4_pinned_relay.directsdk" not in sys.modules


def test_neutral_route_record_allows_one_model_iteration(tmp_path, monkeypatch):
    turn = driver()
    module = ModuleType("hermes_cli.oneshot")
    module._close_agent = lambda *a: None
    monkeypatch.setitem(sys.modules, "hermes_cli.oneshot", module)
    agent = SimpleNamespace(max_iterations=6)
    def conversation(prompt):
        assert prompt == "Say hello." and agent.max_iterations == 1
        return {"final_response": "Hello.", "api_calls": 1, "completed": True}
    agent.run_conversation = conversation
    monkeypatch.setattr(turn, "build_agent", lambda cwd, text=False: (agent, None, {}))
    monkeypatch.setattr(turn, "install_text_capture", lambda *a: ({}, ["fake capture"]))
    monkeypatch.setattr(turn, "skill_tree", lambda: "synthetic-tree")
    value = turn.turn({"text": True, "item_dir": str(tmp_path), "cwd": str(tmp_path),
                       "route_record_path": str(tmp_path / "private.json"), "prompt": "Say hello."})
    assert value["api_calls"] == 1
    assert value["skill_tree"] == "synthetic-tree"


def test_trailing_m1_shapes():
    for shape in ("string", "one-block", "two-block"):
        expected, _, final = relay_fixture()
        record = {"blocks": [{"kind": "M1", "placement": "trailing-system", "text": "Today's date is <date>."}]}
        final["system"] = expected["system"]
        final["messages"] = deepcopy(expected["messages"])
        content = "Today's date is 2026-10-23."
        if shape != "string":
            content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            if shape == "two-block":
                content.append({"type": "text", "text": "extra"})
        final["messages"].append({"role": "system", "content": content})
        assert probe.compare_request(expected, final, record) == (["messages"] if shape == "two-block" else [])


def test_a4b_record_order_and_deferred_trailing():
    expected, _, final = relay_fixture()
    block = lambda kind, place, value: {"kind": kind, "placement": place, "text": value}
    record = {"blocks": [block("M1", "trailing-system", "Today's date is <date>."),
        block("S1", "system-prefix", "Identity."), block("S2", "system-prefix", "a=1"),
        block("S2", "system-prefix", "b=2"), block("M1", "newest-user", "Date: <date>"),
        block("M1", "newest-user", "test@example.invalid"), block("M1", "trailing-system", "test@example.invalid")]}
    final["system"] = [{"type": "text", "text": t} for t in ("Identity.", "a=1", "b=2", "S")]
    final["messages"][0]["content"] = [{"type": "text", "text": t} for t in
        ("item", "Date: 2026-10-23", "test@example.invalid", "R1 exact")]
    final["messages"] += [{"role": "system", "content": t} for t in
        ("Today's date is 2026-10-23.", "test@example.invalid")]
    assert probe.compare_request(expected, final, record) == []


def capture_runtime(monkeypatch):
    turn = driver()
    package, loop, codex = ModuleType("agent"), ModuleType("agent.conversation_loop"), ModuleType("agent.transports.codex")
    loop.build_api_request = lambda subject, **kwargs: None
    package.conversation_loop = loop
    class Transport:
        def preflight_kwargs(self, value):
            return value
    codex.ResponsesApiTransport = Transport
    for name, module in (("agent", package), ("agent.conversation_loop", loop), ("agent.transports.codex", codex)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(turn, "expected_codex", lambda S, M, options: {"instructions": S, "input": M})
    agent = SimpleNamespace(provider="openai-codex", model=items.MODELS[1], _codex_reasoning_replay_enabled=True,
        _current_task_id="synthetic", _tool_result_content_for_active_model=lambda name, result: result)
    send_runtime(monkeypatch, loop, agent)
    return turn, loop, Transport, agent


def send_runtime(monkeypatch, loop, agent):
    agent.api_mode, agent.base_url = "codex_responses", "http://localhost.invalid"
    agent._should_sanitize_tool_calls = lambda: agent.api_mode != "codex_responses"
    def sanitize(row, **kwargs):
        assert kwargs == dict(model=agent.model, base_url=agent.base_url, provider=agent.provider)
        if isinstance(row.get("tool_calls"), list):
            row["tool_calls"] = [{k: v for k, v in call.items() if k not in
                                 {"call_id", "response_item_id", "extra_content"}} for call in row["tool_calls"]]
    agent._sanitize_tool_calls_for_strict_api = sanitize
    def canonicalize(rows):
        for row in rows:
            for call in row.get("tool_calls") or []:
                try:
                    call["function"]["arguments"] = json.dumps(json.loads(call["function"]["arguments"]),
                                                              sort_keys=True, separators=(",", ":"))
                except (ValueError, KeyError):
                    call["function"]["arguments"] = "{}"
    loop._canonicalize_api_tool_calls = canonicalize
    module = ModuleType("agent.message_sanitization")
    def surrogates(rows):
        # Match the pinned sanitizer on the recorded user content (no mutation of keys).
        for row in rows:
            if isinstance(row.get("content"), str):
                row["content"] = re.sub(r"[\ud800-\udfff]", "\ufffd", row["content"])
    module._sanitize_messages_surrogates = surrogates
    monkeypatch.setitem(sys.modules, module.__name__, module)


def read_runtime(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures/a4-skill-results.json").read_text())
    skills, executor, storage, dispatch, tools = [ModuleType(n) for n in
        ("tools.skills_tool", "agent.tool_executor", "tools.tool_result_storage", "agent.tool_dispatch_helpers", "tools")]
    def skill_view(name, file_path=None, task_id=None, preprocess=True):
        assert name == "emotion-chip-voice" and file_path is None
        assert task_id == "synthetic" and preprocess is True
        return fixture["skill_view"]
    def skills_list(category=None, task_id=None):
        assert category is None and task_id == "synthetic"
        return fixture["skills_list"]
    skills.skill_view, skills.skills_list = skill_view, skills_list
    tools.skills_tool = skills
    executor._budget_for_agent = lambda agent: "budget"
    executor.get_active_env = lambda task: "cell-env"
    storage.maybe_persist_tool_result = lambda result, name, call, **kwargs: result
    storage.enforce_turn_budget = lambda rows, **kwargs: rows
    dispatch.make_tool_result_message = lambda name, result, call: {"content": result}
    for module in (skills, executor, storage, dispatch, tools):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return fixture


def test_tool_result_reference_capture(tmp_path, monkeypatch):
    for case in ("normal", "list", "stub", "warning", "manage", "unknown", "bad-args", "preprocess", "invalid-json"):
        with monkeypatch.context() as patch:
            turn, loop, Transport, agent = capture_runtime(patch)
            fixture = read_runtime(patch)
            finals, _ = turn.install_text_capture({"item_dir": str(tmp_path), "prompt": "hello"}, agent)
            def capture(messages, number):
                api = deepcopy(messages)
                if case != "invalid-json":
                    loop._canonicalize_api_tool_calls(api)
                loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + api,
                    api_call_count=number, messages=messages, tools_for_api=[])
                Transport().preflight_kwargs({"model": agent.model, "instructions": "S", "input": api})
            first = [{"role": "user", "content": "hello"}]
            capture(first, 1)
            name = "skills_list" if case == "list" else "unknown" if case == "unknown" else "skill_view"
            arguments = {} if case == "list" else {"name": "emotion-chip-voice"}
            if case == "bad-args":
                arguments["unsupported"] = True
            if case == "preprocess":
                arguments.update(preprocess=False, task_id="ignored")
            function = {"name": name, "arguments": "not-json" if case == "invalid-json" else json.dumps(arguments)}
            calls = [{"id": "read", "function": function}]
            if case == "manage":
                calls.insert(0, {"id": "write", "function": {"name": "skill_manage", "arguments": "{}"}})
            content = fixture["skills_list" if case == "list" else "skill_view"]
            if case == "stub":
                content = "[hermes note: byte-identical result already provided]"
            if case == "warning":
                content += "\n[Tool loop warning: use the result already provided]"
            messages = first + [{"role": "assistant", "tool_calls": calls}]
            if case == "manage":
                messages.append({"role": "tool", "tool_call_id": "write", "content": "created"})
            messages.append({"role": "tool", "tool_call_id": "read", "content": content})
            capture(messages, 2)
            assert finals[2]["history_matches"] is (case in {"normal", "list", "bad-args", "preprocess"})


def test_call_one_item_alone(tmp_path, monkeypatch):
    turn, loop, Transport, agent = capture_runtime(monkeypatch)
    finals, _ = turn.install_text_capture({"item_dir": str(tmp_path), "prompt": "hello"}, agent)
    for content in ("hello", "hello plus note"):
        messages = [{"role": "user", "content": content}]
        loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + messages,
            api_call_count=1, messages=[], tools_for_api=[])
        Transport().preflight_kwargs({"model": agent.model, "instructions": "S", "input": messages})
        assert finals[1]["history_matches"] is (content == "hello")


def test_skill_reads_normalize_content_only(tmp_path, monkeypatch):
    receipts = []
    for index in range(2):
        with monkeypatch.context() as patch:
            turn, loop, Transport, agent = capture_runtime(patch)
            patch.setattr(turn, "tool_results_match", lambda *args: True)
            cell = tmp_path / str(index)
            finals, _ = turn.install_text_capture({"item_dir": str(cell), "prompt": "hello"}, agent)
            call_id = f"call-{index}"
            messages = [{"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "skill_view"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": f"skill from {cell}/home/skills"}]
            loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + messages,
                api_call_count=2, messages=messages, tools_for_api=[])
            Transport().preflight_kwargs({"model": agent.model, "instructions": "S", "input": messages})
            receipts.append(finals[2]["skill_reads"])
    assert receipts[0] == receipts[1] == [{**probe.component("skill from <ITEM>/home/skills"), "tool": "skill_view"}]


def test_skill_tree_drift_fails_check_three(tmp_path, monkeypatch):
    turn = driver()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    paths = [tmp_path / "home" / root / "emotion-chip-voice/SKILL.md" for root in
             ("skills", "plugin-data/emotion-chip/skills")]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text("published skill")
    golden = {"skill_tree": turn.skill_tree(), "system_sha256": "S", "route": {"model": items.MODELS[0]}}
    capture = {"api_call_count": 1, "system": {"sha256": "S"}, "checks": [], "canaries": {},
               "history_matches": True, "model": items.MODELS[0]}
    result = {"skill_tree": "tree", "api_calls": 1, "captures": [capture], "completed": True, "final_response": "reply"}
    for usage in (".usage.json", ".usage.json.lock"):
        (tmp_path / "home/skills" / usage).write_text("synthetic usage")
    assert turn.skill_tree() == golden["skill_tree"]
    for path in paths:
        before = path.read_text()
        result["skill_tree"] = turn.skill_tree()
        assert text.validity(result, golden, [], True)[3]
        path.write_text("changed skill")
        result["skill_tree"] = turn.skill_tree()
        assert not text.validity(result, golden, [], True)[3]
        path.write_text(before)


def test_schema_reference_survives_empty_first_attempt(tmp_path, monkeypatch):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, None)
    harness.golden[model, "OFF"] = {"skill_tree": "tree", "system_sha256": "S", "route": {"model": model},
                                  "tools": ["skill_view"], "sent_tools": ["skill_view"]}
    monkeypatch.setattr(harness, "home", lambda *args: None)
    monkeypatch.setattr(run, "read_jsonl", lambda *args: [])
    monkeypatch.setattr(run, "memories_empty", lambda *args: True)
    monkeypatch.setattr(run, "first_call_tokens", lambda *args: 100)
    capture = {"api_call_count": 1, "system": {"sha256": "S"}, "model": model,
        "checks": [], "canaries": {}, "history_matches": True, "tool_schemas": {"skill_view": "schema"}}
    values = iter([{"error": "x", "rc": "timeout"},
        {"skill_tree": "tree", "api_calls": 1, "captures": [capture], "completed": True, "final_response": "reply"}])
    monkeypatch.setattr(harness, "call", lambda *args, **kwargs: next(values))
    row = {**phase_rows(1)[0], "item": {"id": "0", "user_message": "hello"}}
    result = text.run_phase([row], lambda row, number: harness.execute("reference", row, number), lambda v: None)
    assert result["attempts"][1]["admitted"] and not result["stopped"]
    assert harness.provenance["tool_schemas"][model] == {"skill_view": "schema"}


@pytest.mark.parametrize("scenario", ["outlier", "critical"])
def test_pilot_gate_and_cross_phase_failure_budget(tmp_path, monkeypatch, scenario):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {model: {"owner_approved": True, "identity": {}}}, None, None)
    monkeypatch.setattr(harness, "warm", lambda model: None)
    pilot_first = text.cell_order(True)[0]
    def execute(phase, row, number):
        first = all(row[k] == pilot_first[k] for k in ("set", "item", "condition", "run"))
        bad = (1,) if scenario == "critical" and number == 1 and (phase == "reference" or (phase == "pilot" and first)) else ()
        return attempt(200 if scenario == "outlier" and phase == "pilot" and first else 100, bad)
    monkeypatch.setattr(harness, "execute", execute)
    monkeypatch.setattr(text, "publish_attempt", lambda *args: None)
    result = harness.run_model(model)
    if scenario == "outlier":
        assert any(v["outlier"] for v in result["pilot"]["attempts"])
        assert len(result["full"]["attempts"]) == 684
    else:
        assert result["pilot"]["stopped"] and result["pilot"]["critical_failures"] == 2
        assert "full" not in result


def test_original_median_excludes_immediate_rerun():
    def execute(row, number):
        index = int(row["item"]["id"])
        return attempt(300 if number == 2 else 100 + index, (5,) if index == 0 and number == 1 else ())
    result = text.run_phase(phase_rows(3), execute, lambda v: None)
    assert result["medians"]["OFF"] == 101


def test_stop_preserves_only_aborted_raw_attempts(tmp_path, monkeypatch):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {model: {"owner_approved": True, "identity": {}}}, None, None)
    monkeypatch.setattr(harness, "warm", lambda model: None)
    monkeypatch.setattr(text, "cell_order", lambda pilot=False: phase_rows(4))
    def execute(phase, row, number):
        if phase == "full" and row["item"]["id"] == "2":
            raise RuntimeError("STOP: credential or sign-in need")
        return attempt()
    monkeypatch.setattr(harness, "execute", execute)
    with pytest.raises(RuntimeError, match="STOP: credential"):
        harness.run_model(model)
    records = list((harness.out / "attempts/full").rglob("*.json"))
    assert len(records) == 2
    assert all(items.read_json(p)["aborted"] and items.read_json(p)["admitted"] is False for p in records)
    assert not (harness.out / "generation").exists()
    assert all(4 not in items.read_json(p)["checks"] and "4" not in items.read_json(p)["checks"] for p in records)


@pytest.mark.parametrize("fence", ["```json", "```"])
def test_claude_judge_fenced_answer(tmp_path, fence):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("unchanged rubric and batch")
    batch = [{"id": "synthetic"}]
    result = {"results": [{"id": "synthetic", "sycophantic": False, "harsh": False, "note": ""}]}
    def fake(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": fence + "\n" + json.dumps(result) + "\n```"}))
    answer = judge.claude_judge_batch(prompt, None, None, fake, lambda: "fake")
    assert judge.validate_judgments(answer, batch, "g1") == result
    def malformed(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": fence + "\n{bad json}\n```"}))
    with pytest.raises(RuntimeError, match="invalid output"):
        judge.claude_judge_batch(prompt, None, None, malformed, lambda: "fake")


def test_cross_model_judge_dispatch(tmp_path, monkeypatch):
    for model in items.MODELS:
        for number in items.RUNS:
            row = {"set": "g1", "split": "heldout", "item": items.load_items("g1")[0],
                "model": model, "condition": "OFF", "run": number, "reply": "Lists are mutable."}
            items.atomic_json(gen.generation_path(tmp_path, row), row)
    seen = []
    def recorder(name):
        def call(prompt, schema, output):
            batch = json.loads(prompt.read_text().rsplit("\n\n", 1)[1])
            seen.append((name, {row["id"] for row in batch}))
            return {"results": [{"id": row["id"], "sycophantic": False, "harsh": False, "note": ""} for row in batch]}
        return call
    monkeypatch.setattr(judge, "judge_batch", recorder("codex"))
    monkeypatch.setattr(judge, "claude_judge_batch", recorder("claude"))
    judge.judge("g1", tmp_path)
    mapping = items.read_json(tmp_path / "judge/g1/index.json")["mapping"]
    assert len(seen) == 2
    assert [(name, {mapping[key]["identity"][2] for key in keys}) for name, keys in seen] == [
        ("codex", {"claude-opus-5-5"}), ("claude", {"gpt-6.1-sol"})]


def test_golden_records_skill_tree(tmp_path, monkeypatch):
    turn = driver()
    system, plugins = ModuleType("agent.system_prompt"), ModuleType("hermes_cli.plugins")
    system._frozen_plugin_prompt_sections = lambda agent: []
    system._skills_prompt = lambda agent: ""
    plugins.format_system_prompt_sections = lambda sections: ""
    monkeypatch.setitem(sys.modules, "agent.system_prompt", system)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(turn, "skill_tree", lambda: "synthetic-tree")
    result = turn.golden_delta(None, "S", {"sections": {}, "arm": "OFF", "item_dir": str(tmp_path)})
    assert result["skill_tree"] == "synthetic-tree"


def send_fixture():
    return json.loads((Path(__file__).parent / "fixtures/a4-send-path.json").read_text())


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
def test_pinned_sent_history(tmp_path, monkeypatch, api_mode):
    turn, loop, Transport, agent = capture_runtime(monkeypatch)
    record = send_fixture()["histories"][api_mode]
    agent.api_mode, agent.provider, agent.model = api_mode, record["provider"], record["model"]
    fixture = read_runtime(monkeypatch)
    # Hermes-path output is independent of the expected-history builder under test.
    history, sent = deepcopy(record["history"]), deepcopy(record["sent"])
    history[-1]["content"] = sent[-1]["content"] = fixture["skill_view"]
    original = deepcopy(history)
    assert turn.history_for_send(history, agent) == sent
    assert history == original
    capture = []
    def upstream(item_dir, callback):
        capture.append(callback)
        return ["synthetic relay final"]
    monkeypatch.setattr(turn, "wrap_upstream", upstream)
    monkeypatch.setattr(turn, "expected_relay", lambda S, M, *a, **kw: {"instructions": S, "input": M})
    finals, _ = turn.install_text_capture({"item_dir": str(tmp_path), "prompt": "hello", "provider_path": "synthetic"}, agent)
    def record_call(messages, internal, number):
        loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + messages,
                               messages=internal, api_call_count=number, tools_for_api=[])
        final = {"model": agent.model, "instructions": "S", "input": messages}
        if api_mode == "codex_responses":
            Transport().preflight_kwargs(final)
        else:
            capture[0](final)
    record_call(sent[:1], history[:1], 1)
    for mutation in ("none", "user", "arguments", "stub"):
        actual = deepcopy(sent)
        if mutation == "user":
            actual[0]["content"] += " injected"
        if mutation == "arguments":
            args = json.loads(actual[1]["tool_calls"][0]["function"]["arguments"])
            args["reason"] = "changed"
            actual[1]["tool_calls"][0]["function"]["arguments"] = json.dumps(args, sort_keys=True, separators=(",", ":"))
        if mutation == "stub":
            actual[-1]["content"] = "[hermes note: result already provided]"
        record_call(actual, history, 2)
        assert finals[2]["history_matches"] is (mutation == "none")


@pytest.mark.parametrize("single_query", ["False", "True"])
def test_on_golden_pinned_index(tmp_path, monkeypatch, single_query):
    turn = driver()
    fixture = send_fixture()
    system, plugins = ModuleType("agent.system_prompt"), ModuleType("hermes_cli.plugins")
    system._frozen_plugin_prompt_sections = lambda agent: []
    index = fixture["indexes"][single_query]
    system._skills_prompt = lambda agent: index
    plugins.format_system_prompt_sections = lambda sections: ""
    plugins.get_plugin_manager = lambda: SimpleNamespace(list_plugin_skill_metadata=lambda: [fixture["metadata"]])
    monkeypatch.setitem(sys.modules, system.__name__, system)
    monkeypatch.setitem(sys.modules, plugins.__name__, plugins)
    monkeypatch.setattr(turn, "skill_tree", lambda: "tree")
    loaded_chip_skill(tmp_path, monkeypatch, fixture["skill_text"])
    args = {"sections": {}, "arm": "ON", "item_dir": str(tmp_path)}
    assert turn.golden_delta(None, index + "\n\nS", args)["residue"] == probe.component("S")
    for extra in ("    - second-skill", "    - emotion-chip:emotion-chip-voice: Synthetic delivery guide"):
        index = fixture["indexes"][single_query].replace("</available_skills>", extra + "\n</available_skills>")
        with pytest.raises(RuntimeError, match="golden skill index differs"):
            turn.golden_delta(None, index + "\n\nS", args)


def loaded_chip_skill(tmp_path, monkeypatch, content):
    # Exact directory-plugin namespace from pinned plugins_loader._directory_module_name.
    name = "hermes_plugins.emotion_chip.emotion_chip.hermes.settings"
    settings = ModuleType(name)
    root = tmp_path / "home/plugin-data/emotion-chip"
    path = root / "skills/emotion-chip-voice/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    settings.plugin_data_dir = lambda: root
    for package in ("hermes_plugins", "hermes_plugins.emotion_chip", "hermes_plugins.emotion_chip.emotion_chip",
                    "hermes_plugins.emotion_chip.emotion_chip.hermes"):
        module = ModuleType(package)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, package, module)
    monkeypatch.setitem(sys.modules, name, settings)
    for bare in ("emotion_chip", "emotion_chip.data", "emotion_chip.guide"):
        monkeypatch.setitem(sys.modules, bare, None)
    return path


def test_golden_home_has_empty_skills_root(tmp_path):
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, None)
    model = items.MODELS[0]
    for plugin in text.hermes_config(model, "ON", tmp_path)["plugins"]["enabled"]:
        (tmp_path / "src" / plugin).mkdir(parents=True)
    home = tmp_path / "golden" / "ON"
    harness.home(home, model, "ON")
    assert (home / "home/skills").is_dir()
    assert list((home / "home/skills").iterdir()) == []
    fixture = send_fixture()["index_root_gate"]
    assert fixture["missing"] == "" and fixture["empty_exists"]


def test_guardrail_halt_invalidates_turn(tmp_path, monkeypatch):
    turn = driver()
    guardrail = send_fixture()["guardrail"]
    capture = {"api_call_count": 1, "system": {"sha256": "S"}, "checks": [],
               "canaries": {}, "history_matches": True, "model": items.MODELS[0]}
    module = ModuleType("hermes_cli.oneshot")
    module._close_agent = lambda *a: None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    agent = SimpleNamespace(run_conversation=lambda prompt: {"guardrail": guardrail, "api_calls": 1,
        "completed": True, "final_response": "I stopped retrying."})
    monkeypatch.setattr(turn, "build_agent", lambda *a, **kw: (agent, None, {}))
    monkeypatch.setattr(turn, "install_text_capture", lambda *a: ({1: capture}, []))
    monkeypatch.setattr(turn, "skill_tree", lambda: "tree")
    value = turn.turn({"text": True, "item_dir": str(tmp_path), "cwd": str(tmp_path), "prompt": "hello"})
    assert value["guardrail"] == guardrail
    golden = {"skill_tree": "tree", "system_sha256": "S", "route": {"model": items.MODELS[0]}}
    assert text.validity({**value, "guardrail": None}, golden, [], True)[3]
    checks = text.validity(value, golden, [], True)
    assert checks[1] and checks[2] and checks[5] and checks[3] is False


@pytest.mark.parametrize("marked", [False, True])
def test_reference_equals_recorded_handler_message(monkeypatch, marked):
    turn, _, _, agent = capture_runtime(monkeypatch)
    read_runtime(monkeypatch)
    skills, storage, dispatch = (sys.modules[n] for n in
        ("tools.skills_tool", "tools.tool_result_storage", "agent.tool_dispatch_helpers"))
    for case in send_fixture()["handler_cases"]:
        # The input and oracle are separate recorded fields, from separate pinned calls.
        raw, oracle = case["raw_result"], case["handler_message"]
        assert raw is not oracle
        if case["tool"] == "skill_view":
            def view(name, file_path=None, task_id=None, preprocess=True):
                assert name == case["arguments"]["name"] and file_path is None
                assert task_id == "synthetic" and preprocess is True
                return raw
            skills.skill_view = view
        else:
            def listing(category=None, task_id=None):
                assert category is None and task_id == "synthetic"
                return raw
            skills.skills_list = listing
        def persist(value, name, call, *, env, config):
            assert (value, name, call, env, config) == (raw, case["tool"], "read", "cell-env", "budget")
            return value + ("|P" if marked else "")
        def wrap(name, value, call):
            assert (name, value, call) == (case["tool"], raw + ("|P" if marked else ""), "read")
            return {"content": value + ("|W" if marked else "")}
        def budget(rows, *, env, config):
            assert (env, config) == ("cell-env", "budget")
            for row in rows:
                row["content"] += "|B" if marked else ""
        storage.maybe_persist_tool_result, storage.enforce_turn_budget = persist, budget
        dispatch.make_tool_result_message = wrap
        function = {"name": case["tool"], "arguments": json.dumps(case["arguments"])}
        assert turn.tool_reference(function, "read", agent)["content"] == oracle + ("|P|W" if marked else "")
        messages = [{"role": "assistant", "tool_calls": [{"id": "read", "function": function}]},
                    {"role": "tool", "tool_call_id": "read", "content": oracle + ("|P|W|B" if marked else "")}]
        assert turn.tool_results_match(messages, agent)


@pytest.mark.parametrize("bad_field", ["system", "model"])
def test_schema_reference_requires_other_check_two_conditions(tmp_path, monkeypatch, bad_field):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, None)
    harness.golden[model, "OFF"] = {"skill_tree": "tree", "system_sha256": "S", "route": {"model": model},
                                  "tools": ["skill_view"], "sent_tools": ["skill_view"]}
    for name, fake in (("read_jsonl", lambda *a: []), ("memories_empty", lambda *a: True),
                       ("first_call_tokens", lambda *a: 100)):
        monkeypatch.setattr(run, name, fake)
    monkeypatch.setattr(harness, "home", lambda *a: None)
    good = {"api_call_count": 1, "system": {"sha256": "S"}, "model": model, "checks": [],
            "canaries": {}, "history_matches": True, "tool_schemas": {"skill_view": "good"}}
    bad = {**good, bad_field: {"sha256": "BAD"} if bad_field == "system" else "BAD",
           "tool_schemas": {"skill_view": "bad"}}
    values = iter({"captures": [capture], "skill_tree": "tree", "api_calls": 1, "completed": True,
                   "final_response": "reply"} for capture in (bad, good))
    monkeypatch.setattr(harness, "call", lambda *a, **kw: next(values))
    row = {**phase_rows(1)[0], "item": {"id": "0", "user_message": "hello"}}
    result = text.run_phase([row], lambda r, n: harness.execute("reference", r, n), lambda value: None)
    assert result["attempts"][0]["checks"][2] is False
    assert len(result["attempts"]) == 2 and result["attempts"][1]["admitted"]
    assert result["critical_failures"] == 1 and not result["stopped"]
    assert harness.provenance["tool_schemas"][model] == {"skill_view": "good"}


def test_skill_manage_poisoning_branch(monkeypatch):
    turn, _, _, agent = capture_runtime(monkeypatch)
    fixture = read_runtime(monkeypatch)
    read = {"id": "r", "function": {"name": "skill_view", "arguments": '{"name":"emotion-chip-voice"}'}}
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "tool_calls": [read]},
                {"role": "tool", "tool_call_id": "r", "content": fixture["skill_view"]}]
    assert turn.tool_results_match(messages, agent)
    # Direct helper invocation isolates poisoning from capture's independent ID pairing check.
    # No result for w: the later read has a valid reference and fails only due to poisoning.
    messages[1]["tool_calls"].insert(0, {"id": "w", "function": {"name": "skill_manage", "arguments": "{}"}})
    assert turn.tool_results_match(messages, agent) is False


@pytest.mark.parametrize("model", items.MODELS)
def test_harness_warm_host_args_and_checks(tmp_path, monkeypatch, model):
    calls, homes, scenario = [], [], {"kind": "valid"}
    class FakeHost:
        def __init__(self, *args):
            pass
        def call(self, item, args, **kwargs):
            arm = item.name
            assert args["mode"] == "warm" and args["text"] is True
            assert args["arm"] == arm and args["sections"] == text.sections(model, arm)
            assert args["item_dir"] == str(item)
            assert kwargs == {"token": None, "timeout": 600}
            calls.append(arm)
            return {"canaries": {"synthetic": scenario["kind"] == "canary"},
                    "tools": ["skill_manage", "skill_view", "skills_list"] + (["terminal"] if scenario["kind"] == "tools" else []),
                    **({} if scenario["kind"] == "sent-missing" else {"sent_tools": {
                        "sent-short": ["one"], "sent-duplicate": ["one"] * 3,
                        "sent-not-list": "one", "sent-not-name": ["one", "two", None],
                        "sent-empty-name": ["one", "two", ""],
                    }.get(scenario["kind"], relay_tools_fixture()["sent_tools"] if model == items.MODELS[0]
                          else ["skill_manage", "skill_view", "skills_list"])}),
                    "error": "synthetic" if scenario["kind"] == "error" else None,
                    "residue": {"sha256": arm if scenario["kind"] == "residue" else "same"}}
    monkeypatch.setattr(run, "Host", FakeHost)
    monkeypatch.setattr(run, "db_summary", lambda path: {
        "sessions": int(scenario["kind"] == "sessions"), "usage_rows": int(scenario["kind"] == "usage")})
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, lambda: None)
    monkeypatch.setattr(harness, "home", lambda item, m, a: homes.append((item, m, a)))
    harness.warm(model)
    assert calls == list(items.CONDITIONS)
    assert [(m, a) for _, m, a in homes] == [(model, a) for a in items.CONDITIONS]
    assert set(harness.golden) == {(model, a) for a in items.CONDITIONS}
    assert all(v["canaries"] == {"marker-1": False} for v in harness.golden.values())
    for kind in ("error", "canary", "sessions", "usage", "residue", "tools", "sent-missing", "sent-short",
                 "sent-duplicate", "sent-not-list", "sent-not-name", "sent-empty-name"):
        scenario["kind"] = kind
        with pytest.raises(RuntimeError, match="golden arm deltas" if kind == "residue" else "golden warm failed"):
            harness.warm(model)


def test_text_paths_expand_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    example = items.read_json(run.HERE / "paths.example.json")
    assert {"claude_command", "claude_code_version", "harness_auth_home"} <= example.keys()
    assert example["claude_command"].startswith("~/") and example["harness_auth_home"].startswith("~/")
    raw = {**example, "claude_code_version": "2.1.synthetic", "canaries": ["~/source", "tree-name", " "]}
    config = tmp_path / "paths.json"
    config.write_text(json.dumps(raw))
    paths = text.load_paths("~/paths.json")
    for key in (set(run.PATH_KEYS) - {"host_label"}) | {"claude_command", "harness_auth_home", "eleven_key_file"}:
        assert paths[key] == tmp_path / raw[key][2:]
    assert paths["canaries"] == [str(tmp_path / "source"), "tree-name"]
    assert paths["host_label"] == raw["host_label"] and paths["claude_code_version"] == "2.1.synthetic"


def test_main_missing_hermes_import_explains_python(tmp_path, monkeypatch, bench_temp_root):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "paths.json"
    config.write_text(json.dumps({"host": "~/pinned-host", "harness_auth_home": "~/parent-home"}))
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", None)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("HERMES_HOME", "synthetic-unused")
    with pytest.raises(SystemExit, match="Hermes imports unavailable") as error:
        text.main(["--paths", "~/paths.json", "--scratch", str(tmp_path / "scratch"), "--out", str(tmp_path / "out")])
    assert f"{tmp_path}/pinned-host/.venv/bin/python -m bench.hermes.text" in str(error.value)
    assert "<host>/.venv/bin/python -m bench.hermes.text" in text.__doc__
    assert text.os.environ["HERMES_HOME"] == str(tmp_path / "parent-home")


def test_golden_uses_loaded_namespace_and_served_skill(tmp_path, monkeypatch):
    turn = driver()
    fixture = send_fixture()
    system, plugins = ModuleType("agent.system_prompt"), ModuleType("hermes_cli.plugins")
    system._frozen_plugin_prompt_sections = lambda agent: []
    system._skills_prompt = lambda agent: fixture["indexes"]["False"]
    plugins.format_system_prompt_sections = lambda sections: ""
    plugins.get_plugin_manager = lambda: SimpleNamespace(list_plugin_skill_metadata=lambda: [fixture["metadata"]])
    monkeypatch.setitem(sys.modules, system.__name__, system)
    monkeypatch.setitem(sys.modules, plugins.__name__, plugins)
    monkeypatch.setattr(turn, "skill_tree", lambda: "tree")
    skill = loaded_chip_skill(tmp_path, monkeypatch, "Synthetic served skill.\n")
    args = {"sections": {}, "arm": "ON", "item_dir": str(tmp_path)}
    for content in ("Synthetic served skill.\n", "Changed served skill.\n\n"):
        skill.write_text(content, encoding="utf-8")
        result = turn.golden_delta(None, fixture["indexes"]["False"] + "\n\nS", args)
        assert result["skill"] == probe.component(content)
        assert result["skill"] != probe.component(content.rstrip())
    monkeypatch.setitem(sys.modules, "hermes_plugins.emotion_chip.emotion_chip.hermes.settings", None)
    with pytest.raises(ImportError):
        turn.golden_delta(None, fixture["indexes"]["False"] + "\n\nS", args)


@pytest.mark.parametrize("route", ["codex", "relay"])
def test_message_hashes_normalize_only_item_content(route):
    rows = []
    for attempt in ("/synthetic/cell/1-1", "/synthetic/cell/1-2"):
        messages = [{"role": "tool", "tool_call_id": "read", "content": json.dumps({"path": attempt + "/SKILL.md"})}]
        final_message = {"role": "user", "content": [{"type": "text", "text": attempt + "/SKILL.md"}]}
        final = ({"instructions": "S", "input": [final_message]} if route == "codex" else
                 {"system": "S", "messages": [final_message]})
        output = {"type": "function_call_output", "call_id": "read", "output": messages[0]["content"]}
        if route == "codex":
            final["input"].append(output)
        expected = {"instructions": "S", "input": [final_message]}
        row = probe.capture_row("S", messages, final, expected, {}, attempt)
        assert row["messages"] == [probe.component({**messages[0], "content": json.dumps({"path": "<ITEM>/SKILL.md"})})]
        wanted = [probe.component({**final_message, "content": [{"type": "text", "text": "<ITEM>/SKILL.md"}]})]
        if route == "codex":
            wanted.append(probe.component({**output, "output": json.dumps({"path": "<ITEM>/SKILL.md"})}))
        assert row["final_messages"] == wanted
        rows.append(row)
    assert rows[0]["messages"] == rows[1]["messages"]
    assert rows[0]["final_messages"] == rows[1]["final_messages"]
    original = {"role": "assistant", "content": "Session ID: keep this", "tool_calls": [{"id": "/cell/id"}]}
    assert probe.message_component(original, "/cell") == probe.component(original)


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
def test_pinned_surrogate_history(monkeypatch, api_mode):
    turn, loop, Transport, agent = capture_runtime(monkeypatch)
    record = send_fixture()["surrogate_histories"][api_mode]
    agent.api_mode, agent.provider, agent.model = api_mode, record["provider"], record["model"]
    history = deepcopy(record["history"])
    assert history[0]["content"] == "hello\ud800" and record["sent"][0]["content"] == "hello\ufffd"
    assert turn.history_for_send(history, agent) == record["sent"]
    assert history == record["history"]


def test_non_dict_tool_arguments_have_no_reference(monkeypatch):
    turn, loop, Transport, agent = capture_runtime(monkeypatch)
    read_runtime(monkeypatch)
    for name in ("skills_list", "skill_view"):
        for arguments in ("[]", "null", "1", '"text"'):
            assert turn.tool_reference({"name": name, "arguments": arguments}, "read", agent) is None


@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize("model", items.MODELS)
@pytest.mark.parametrize("mutation", ["none", "extra-first", "extra-later", "extra-all", "wrong-route"])
def test_capture_tool_names_match_golden_before_reference(tmp_path, monkeypatch, referenced, model, mutation):
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, None)
    names = ["skill_manage", "skill_view", "skills_list"]
    sent = relay_tools_fixture()["sent_tools"] if model == items.MODELS[0] else names
    schemas = {name: "schema-" + name for name in sent}
    harness.golden[model, "OFF"] = {"skill_tree": "tree", "system_sha256": "S",
                                   "route": {"model": model}, "tools": names, "sent_tools": sent}
    for name, fake in (("read_jsonl", lambda *a: []), ("memories_empty", lambda *a: True),
                       ("first_call_tokens", lambda *a: 100)):
        monkeypatch.setattr(run, name, fake)
    monkeypatch.setattr(harness, "home", lambda *a: None)
    captures = [{"api_call_count": n, "system": {"sha256": "S"}, "model": model,
                 "checks": [], "canaries": {}, "history_matches": True, "tool_schemas": dict(schemas)}
                for n in (1, 2)]
    monkeypatch.setattr(harness, "call", lambda *a, **kw: {
        "captures": deepcopy(captures), "skill_tree": "tree", "api_calls": 2,
        "completed": True, "final_response": "reply"})
    row = {**phase_rows(1)[0], "model": model, "item": {"id": "0", "user_message": "hello"}}
    if referenced:
        assert harness.execute("reference", row, 1)["checks"][2]
        assert harness.provenance["tool_schemas"][model] == schemas
    if mutation == "wrong-route":
        wrong = names if model == items.MODELS[0] else relay_tools_fixture()["sent_tools"]
        for capture in captures:
            capture["tool_schemas"] = {name: "schema-" + name for name in wrong}
    elif mutation.startswith("extra"):
        for index in ({"extra-first": [0], "extra-later": [1], "extra-all": [0, 1]}[mutation]):
            captures[index]["tool_schemas"]["terminal"] = "unexpected-schema"
    assert harness.execute("reference", row, 2)["checks"][2] is (mutation == "none")
    if referenced or mutation == "none":
        assert harness.provenance["tool_schemas"][model] == schemas
    else:
        assert "tool_schemas" not in harness.provenance


def fake_main_runtime(tmp_path, monkeypatch):
    auth, config = ModuleType("hermes_cli.auth"), ModuleType("hermes_cli.config")
    def unused(*args, **kwargs):
        pytest.fail("credential access is forbidden")
    auth._read_codex_tokens = auth.resolve_codex_runtime_credentials = unused
    config.load_config = lambda: {"auth": {"adopt_external_logins": False}}
    for module in (auth, config):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(text, "load_paths", lambda path: {
        "host": tmp_path / "host", "harness_auth_home": tmp_path / "parent"})
    monkeypatch.setattr(text.Harness, "prepare", lambda self: None)
    return ["--paths", str(tmp_path / "unused.json"), "--scratch", str(tmp_path / "scratch"),
            "--out", str(tmp_path / "out")]


@pytest.mark.parametrize("error,message", [
    *[(RuntimeError(value), value) for value in (
        "STOP: credential or sign-in need", "golden warm failed",
        "golden arm deltas differ from published inputs", "pinned provider copy differs",
        "pinned Claude Code binary changed", "Claude Code changed; a new route record is required",
        "credential lifetime is too short", "harness parent must disable external login adoption")],
    (ValueError("synthetic private token and path"), None),
    (RuntimeError("STOP: synthetic third-party private detail"), None),
])
def test_main_model_exception_isolation(tmp_path, monkeypatch, bench_temp_root, error, message):
    argv = fake_main_runtime(tmp_path, monkeypatch)
    seen = []
    def run_model(self, model, route_record):
        seen.append(model)
        assert route_record is False
        if model == items.MODELS[0]:
            raise error
        row = {**phase_rows(1)[0], "model": model, "split": "heldout", "item": items.load_items("g1")[0]}
        value = {**attempt(), "row": row, "admitted": True, "attempt": 1}
        text.publish_attempt(self.out, "full", value)
        return {"full": {"attempts": [value]}}
    monkeypatch.setattr(text.Harness, "run_model", run_model)
    text.main(argv)
    assert seen == list(items.MODELS)
    for s in items.SETS:
        manifest = items.read_json(tmp_path / "out/manifests" / f"{s}.json")
        assert manifest["models"] == [items.MODELS[1]]
        assert set(manifest["phases"]) == {items.MODELS[1]}
        assert manifest["model_failures"] == {items.MODELS[0]: {
            "error": type(error).__name__, "message": message}}
        assert len(manifest["validity_table"]) == 1
    replies = judge.load_replies(tmp_path / "out", "g1")
    assert len(replies) == 1 and replies[0]["model"] == items.MODELS[1]
    assert len(list((tmp_path / "out/attempts").rglob("*.json"))) == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(17)])
def test_main_non_exception_propagates(tmp_path, monkeypatch, bench_temp_root, error):
    argv = fake_main_runtime(tmp_path, monkeypatch)
    seen = []
    def run_model(self, model, route_record):
        seen.append(model)
        raise error
    monkeypatch.setattr(text.Harness, "run_model", run_model)
    with pytest.raises(type(error)):
        text.main(argv)
    assert seen == [items.MODELS[0]]
    assert not (tmp_path / "out/manifests").exists()


def test_file_sha256_streams_file_bytes(tmp_path):
    content = bytes(range(256)) * 8193
    path = tmp_path / "synthetic-binary"
    path.write_bytes(content)
    assert text.file_sha256(path) == hashlib.sha256(content).hexdigest()


def test_call_pins_binary_bytes(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-binary"
    path.write_bytes(b"fake binary; never executed")
    harness = text.Harness(tmp_path, tmp_path / "out", {"claude_command": path}, {}, None, lambda: None)
    harness.provenance["claude_binary_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []
    def call(item, args, **kwargs):
        calls.append(args)
        return {"fake": "result"}
    monkeypatch.setattr(harness.host, "call", call)
    assert harness.call(tmp_path, items.MODELS[0], "OFF") == {"fake": "result"}
    assert len(calls) == 1
    path.write_bytes(b"changed fake binary; never executed")
    with pytest.raises(RuntimeError, match="pinned Claude Code binary changed"):
        harness.call(tmp_path, items.MODELS[0], "OFF")
    assert len(calls) == 1


def route_request(arm, item, date="2026-10-23"):
    instance = json.loads((Path(__file__).parent / "fixtures/a5-e1-block.json").read_text())
    env = instance["instance"].replace("/bench-runs/run-1/cells/reference/claude-opus-5-5/g1/OFF/g1-h01/1-1",
                                       str(item)).replace("2026-10-23", date)
    return {"model": items.MODELS[0], "metadata": {"user_id": f"synthetic-session-{arm}"},
            "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=9.9.9; cc_entrypoint=sdk;"},
                       {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
                       {"type": "text", "text": f"S for {arm}\nhome {item}/home\nConversation started: {date}",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Say hello."},
                                                      {"type": "text", "text": "R1 synthetic reminder"}]},
                         {"role": "system", "content": env}]}


@pytest.mark.parametrize("case", ["same", "extra-block", "unknown-s", "two-s"])
def test_route_record_runs_every_arm(tmp_path, monkeypatch, case):
    model = items.MODELS[0]
    harness = text.Harness(tmp_path, tmp_path / "out", {}, {}, None, lambda: None)
    monkeypatch.setattr(harness, "warm", lambda m: harness.golden.update({(m, a): {
        "system_sha256": probe.digest(f"S for {a}\nhome <ITEM>/home"), "route": {"model": m}, "skill_tree": "tree",
        "sent_tools": []} for a in items.CONDITIONS}))
    monkeypatch.setattr(harness, "home", lambda *a: None)
    for name, fake in (("read_jsonl", lambda *a: []), ("memories_empty", lambda *a: True),
                       ("first_call_tokens", lambda *a: 100)):
        monkeypatch.setattr(run, name, fake)
    seen = []
    class FakeHost:
        def call(self, item, args, **kwargs):
            arm = args["arm"]
            seen.append(arm)
            assert args["item_dir"] == str(item) and args["prompt"] == "Say hello."
            request = route_request(arm, item, "2026-10-24" if arm == "ON" else "2026-10-23")
            if case == "extra-block" and arm == "NATIVE":
                request["messages"].append({"role": "system", "content": "extra"})
            if case == "unknown-s" and arm == "ON":
                request["system"][2]["text"] = "changed " + request["system"][2]["text"]
            if case == "two-s" and arm == "OFF":
                request["system"].append(request["system"][2])
            fd = os.open(args["route_record_path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(probe.canonical(request))
            return {"captures": [], "api_calls": 1, "completed": True, "final_response": "Hello."}
    harness.host = FakeHost()
    result = harness.run_model(model, route_record=True)
    assert seen == list(items.CONDITIONS)
    assert sorted(p.name for p in tmp_path.glob("*.private.json")) == sorted(
        f"{model}-{a}-request.private.json" for a in items.CONDITIONS)
    assert set(result) == {"route_record", "route_arm_invariant", "additions_sha256"}
    assert result["route_record"] == "PRIVATE owner inspection required"
    assert result["route_arm_invariant"] is (case == "same")
    hashes = result["additions_sha256"]
    assert set(hashes) == set(items.CONDITIONS)
    missing = {"unknown-s": "ON", "two-s": "OFF"}.get(case)
    assert all((v is None) if a == missing else re.fullmatch("[0-9a-f]{64}", v) for a, v in hashes.items())
    dumped = json.dumps(result)
    assert all(word not in dumped for word in ("Say hello", "Environment", "billing", str(tmp_path)))


def test_route_additions_scrub_item_and_date(tmp_path):
    first, second = tmp_path / "OFF", tmp_path / "ON"
    sha = lambda arm: probe.digest(f"S for {arm}\nhome <ITEM>/home")
    a = text.route_additions(route_request("OFF", first), first, sha("OFF"))
    b = text.route_additions(route_request("ON", second, "2027-01-02"), second, sha("ON"))
    assert a == b and "<ITEM>/tmp/claude-directsdk-cwd-501" in a and "Today's date is <date>." in a
    assert "S for" not in a and str(tmp_path) not in a
    codex = {"instructions": "S for OFF\nhome x/home", "input": [], "prompt_cache_key": "k-1",
             "extra_headers": {"session_id": "s-1"}}
    assert text.route_additions(codex, "x", sha("OFF")) == '{"input":[]}'
    assert text.route_additions(route_request("OFF", first), first, sha("ON")) is None


def test_route_record_keeps_first_api_call(tmp_path, monkeypatch):
    turn, loop, Transport, agent = capture_runtime(monkeypatch)
    path = tmp_path / "private.json"
    turn.install_text_capture({"item_dir": str(tmp_path), "prompt": "hello", "route_record_path": str(path)}, agent)
    for call, marker in ((1, "first-a"), (1, "first-b"), (2, "second")):
        loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}],
                               api_call_count=call, messages=[], tools_for_api=[])
        Transport().preflight_kwargs({"model": agent.model, "instructions": "S", "marker": marker,
                                      "input": [{"role": "user", "content": "hello"}]})
    assert json.loads(path.read_text())["marker"] == "first-b"
    assert path.stat().st_mode & 0o777 == 0o600
