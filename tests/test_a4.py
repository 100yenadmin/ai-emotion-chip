"""A4 offline contracts: no installed Hermes, relay, credentials, or model processes."""
from copy import deepcopy
import importlib.util
import inspect
import json
from pathlib import Path
import random
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
        {"id": "call", "function": {"name": "skill_view", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call", "content": "frozen tool result"}]
    for corrupted in (False, True):
        messages = deepcopy(history)
        if corrupted:
            messages[0]["content"] += " injected later"
        loop.build_api_request(agent, api_messages=[{"role": "system", "content": "S"}] + messages,
            api_call_count=2, messages=messages, tools_for_api=[])
        Transport().preflight_kwargs({**value, "input": messages})
        assert finals[2]["history_matches"] is (not corrupted)


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
    result = {"api_calls": 1, "captures": [capture], "completed": True, "final_response": "reply"}
    memory = True
    if check == 1:
        capture["canaries"]["marker-1"] = True
    elif check == 2:
        capture["checks"] = ["messages"]
    elif check == 3:
        capture["history_matches"] = False
    else:
        memory = False
    golden = {"system_sha256": "gold", "route": {"model": items.MODELS[0]}}
    checks = text.validity(result, golden, [], memory)
    assert checks[check] is False and sum(checks.values()) == 3
    assert not any(text.validity({}, golden, [], False).values())


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
        "access_token": "fake-access", "refresh_token": "fake-refresh"}
    with pytest.raises(ValueError, match="isolated"):
        manager.provision(tmp_path / "profiles" / "home")


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
        assert command == list(judge.CLAUDE_JUDGE_COMMAND)
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
    value = turn.turn({"text": True, "item_dir": str(tmp_path), "cwd": str(tmp_path),
                       "route_record_path": str(tmp_path / "private.json"), "prompt": "Say hello."})
    assert value["api_calls"] == 1
