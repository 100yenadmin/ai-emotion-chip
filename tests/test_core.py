"""M0 contract tests: fixture-only, offline and deterministic."""
from dataclasses import replace
import importlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from emotion_chip import __version__
from emotion_chip.consistency import check
from emotion_chip.data import DIALECTS, LITERALS, load_data, validate
from emotion_chip.guide import build_prompt_section, build_skill
from emotion_chip.model import Cue, Dropped, Event, Options, Span, Speaker, Text
from emotion_chip.policy import apply_policy
from emotion_chip.render import dialect_for, render
from emotion_chip.score import header_cue, parse
from conftest import FIXTURES, GOLDENS

spec = importlib.util.spec_from_file_location("hermes_normalize", Path(__file__).parent / "vendor" / "hermes_tts_text_normalize.py")
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)


@pytest.mark.parametrize("case", GOLDENS, ids=lambda c: c["name"])
def test_golden(case, data):
    out = render(case["input"], case["dialect"], case["options"], data)
    assert out.text == case["expected_text"]
    assert out.params == case["expected_params"]
    assert [drop.kind for drop in out.dropped] == case["expected_dropped_kinds"]
    for note in case.get("expected_notes", []):
        assert note in out.notes
    assert out.dialect_version == "2026-10-22"
    assert not any(bad in out.text for bad in ("[[", "[/", "[]"))
    assert all(s.params == out.params and s.t0 is None and s.t1 is None for s in out.plan.segments)


@pytest.mark.parametrize("case", [c for c in GOLDENS if c["dialect"] not in {"tagless", "prose"}], ids=lambda c: c["name"])
def test_idempotence(case, data):
    first = render(case["input"], case["dialect"], case["options"], data)
    assert render(first.text, case["dialect"], case["options"], data).text == first.text


def normalized(s):
    return re.sub(r"\s+", " ", s).strip().rstrip(".")


def structure(score):
    def token(t):
        if isinstance(t, Text):
            return ("text", normalized(t.s))
        if isinstance(t, Cue):
            return ("cue", t.parts, t.words, t.rung)
        if isinstance(t, Span):
            return ("span", t.op, normalized(t.text))
        return (type(t).__name__, t.id)
    return (score.header.fields if score.header else None,
            [[token(t) for t in s.tokens if not isinstance(t, Text) or normalized(t.s)] for s in score.sentences])


@pytest.mark.parametrize("case", GOLDENS, ids=lambda c: c["name"])
def test_cleanup_roundtrip(case):
    cleaned = normalizer.prepare_spoken_text(case["input"])
    assert structure(parse(cleaned)) == structure(parse(case["input"]))


def test_cleanup_positive_control():
    text = "<soft>x</soft>"
    assert structure(parse(normalizer.prepare_spoken_text(text))) != structure(parse(text))


@pytest.mark.parametrize("case", GOLDENS, ids=lambda c: c["name"])
def test_streaming_cuts(case, data):
    full = render(case["input"], case["dialect"], case["options"], data)
    options = {**case["options"], "params_from": full.params}
    for piece in re.compile(r"(?<=[.!?])(?:\s|\n)|(?:\n\n)").split(case["input"]):
        out = render(piece, case["dialect"], options, data)
        assert out.params == full.params
        assert not re.search(r"[\[\]]", re.sub(r"\[[^\[\]]+\]", "", out.text))
        assert not re.search(r"\[(?:soft|stress|slow|fast|whisper|smiling|emphasis|forceful):", out.text)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_preservation(dialect, data):
    text = "[sad] There are 12 retries, not 13; don't retry. Never change example.com/a?q=2. [stress: not 42!]"
    out = render(text, dialect, data=data)
    for value in ("12", "13", "not 13", "don't", "Never", "example.com/a?q=2"):
        assert value in out.text
    assert ("NOT 42!" if dialect == "eleven_v4" else "not 42!") in out.text
    assert ".." not in render("[stress: X 12!]", dialect, data=data).text


@pytest.mark.parametrize("case", GOLDENS, ids=lambda c: c["name"])
def test_consistency(case, data):
    score = parse(case["input"])
    assert check(score, render(score, case["dialect"], case["options"], data), data) == []


def test_mutation_detected(data, monkeypatch):
    monkeypatch.setitem(data.dialects["fish_s2"]["spans"], "soft", "[angry] {text}")
    score = parse("[soft: Hello.]")
    findings = check(score, render(score, "fish_s2", data=data), data)
    assert [(f.kind, f.value) for f in findings] == [("added", "angry")]
    with pytest.raises(ValueError, match="whitelist"):
        validate(data)


@pytest.mark.parametrize("case", GOLDENS, ids=lambda c: c["name"])
def test_emitted_phrase_whitelist(case, data):
    table = data.dialects[case["dialect"]]
    out = render(case["input"], case["dialect"], case["options"], data)
    score = parse(case["input"])
    authored = {"["+t.raw+"]" for s in score.sentences for t in s.tokens if isinstance(t, Cue)}
    authored |= {"[" + header_cue(score.header).raw + "]"} if score.header else set()
    for literal in LITERALS.findall(out.text):
        assert literal in authored or literal in table["phrases"]


def fixture_copy(tmp_path):
    target = tmp_path / "data"
    shutil.copytree(FIXTURES, target)
    return target


@pytest.mark.parametrize("damage", ["missing-group", "missing-tier", "wrong-tier", "unknown-group", "missing-phrase"])
def test_schema_rejections(tmp_path, damage):
    root = fixture_copy(tmp_path)
    name = "groups.yaml" if damage in {"missing-tier", "wrong-tier"} else "emotions.yaml"
    if damage == "missing-phrase":
        name = "dialects/fish_s2.yaml"
    path = root / name
    value = yaml.safe_load(path.read_text())
    if damage == "missing-group":
        value[0].pop("group")
    elif damage == "unknown-group":
        value[0]["group"] = "missing"
    elif damage == "missing-tier":
        value["groups"]["happiness"].pop("tier")
    elif damage == "wrong-tier":
        value["groups"]["happiness"]["tier"] = "A"
    else:
        value["phrases"].remove("[soft tone]")
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):
        load_data(root)


def test_data_cached(data):
    assert data is load_data(FIXTURES)
    assert len(data.groups) == 19
    assert data.lookup("angry")["group"] == "hot_anger"
    assert data.lookup("frustrated")["group"] == "hot_anger"
    assert data.lookup("unknown") is None
    assert validate(data) is data


@pytest.mark.parametrize("dialect", DIALECTS)
def test_guide(dialect, data):
    section = build_prompt_section(dialect, "test-model", data)
    assert len(section) <= 3800
    assert "{{" not in section
    assert "Synthetic model note." in section
    assert "Agent-authored delivery only." in section
    assert "<!--" not in section
    assert build_skill(dialect, "not-present", data).startswith("# Delivery score")
    assert "Synthetic model note." not in build_prompt_section(dialect, "absent", data)


def test_optional_blocks_last_first(data, tmp_path):
    root = fixture_copy(tmp_path)
    local = load_data(root)
    path = root / "guide" / "section.md"
    path.write_text("x"*3700 + "<!-- optional -->FIRST<!-- /optional -->" + "<!-- optional -->" + "y"*200 + "<!-- /optional -->")
    text = build_prompt_section("fish_s2", "absent", local)
    assert text.endswith("FIRST") and "y" not in text
    path.write_text("x"*3801 + "<!-- optional -->tail<!-- /optional -->")
    with pytest.raises(ValueError, match="3800"):
        build_prompt_section("fish_s2", "absent", local)


@pytest.mark.parametrize("backend,models,expected", [
    ("fish", ["s2.1-pro", "s2.1-pro-free", "s2-pro"], "fish_s2"),
    ("fish", ["s1"], "tagless"),
    ("fish", ["drama-3", "drama-3-preview"], "fish_drama3"),
    ("eleven", ["eleven_v4", "eleven_v4_turbo", "eleven_v3", "eleven_v3_conversational"], "eleven_v4"),
    ("eleven", ["eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5"], "tagless"),
])
def test_routing(backend, models, expected, data):
    for model in models:
        dialect = dialect_for(backend, model)
        assert dialect == expected
        if expected == "eleven_v4":
            assert dialect.variant == model
        assert render("Hi.", dialect, data=data).dialect == expected


def test_unknown_routing(data):
    out = render("[sad] Hello.", dialect_for("unknown", "new"), data=data)
    assert out.dialect == "tagless" and out.notes


def test_parser_and_plan(data):
    score = parse("  [[register: warm; pace: steady]]. [VERY SAD, curious] [SOFT: Do not stop 12!] [LAUGH] [Speaker: 2]")
    assert score.header.fields == {"register":"warm", "pace":"steady"}
    tokens = [t for s in score.sentences for t in s.tokens if not isinstance(t, Text)]
    assert tokens[0] == Cue("VERY SAD, curious", ["VERY SAD", "curious"], "very", ["sad", "curious"])
    assert tokens[1].op == "soft" and tokens[1].text == "Do not stop 12!"
    assert isinstance(tokens[2], Event) and tokens[2].id == "laugh"
    assert isinstance(tokens[3], Speaker) and tokens[3].id == "2"
    out = render(score, "fish_drama3", Options(allow_speakers=True), data)
    segment = out.plan.segments[0]
    assert segment.text == "Do not stop 12!"
    assert segment.cues == tokens[:1] and segment.spans == tokens[1:2] and segment.events == []
    # The span's "!" ends its sentence, so the laugh opens the next one, as it would after plain text.
    assert out.plan.segments[1].events == tokens[2:3]
    assert segment.group == "sadness" and segment.rung == "very"


def test_pass_through(data):
    text = "<soft>Do not stop.</soft> <|speaker:0|> CAPS …"
    for dialect in DIALECTS:
        assert render(text, dialect, data=data).text == text


def test_policy_keeps_everything_unless_sounds_are_off(data):
    source = "[crying, happy] [laugh] [sigh] [breath] One. [breath] Two. [breath:shaky] Three."
    result, dropped = apply_policy(parse(source), data)
    assert dropped == [] and result == parse(source)
    result, dropped = apply_policy(parse(source), data, {"sounds": False})
    assert [(x.kind, x.value, x.reason) for x in dropped] == [("event", "laugh", "sounds off"), ("event", "sigh", "sounds off")]
    assert result.sentences[0].tokens[0].raw == "crying, happy"
    with pytest.raises(ValueError):
        apply_policy(parse("Hi"), data, {"sounds": 0})


def test_parameter_boundaries(data):
    assert render("Hi.", "fish_s2", {"overlay":{"fish_speed_offset":-10}}, data).params == {"speed":.85}
    assert render("Hi.", "eleven_v4", {"eleven_stability":-1}, data).params["stability"] == 0
    with pytest.raises(ValueError, match="params_from"):
        render("Hi.", "fish_s2", {"params_from":"missing"}, data)
    assert render("[sad] Hi.", "tagless", data=data).params == {}


def test_cli(monkeypatch, capsys, data):
    cli = importlib.import_module("emotion_chip.cli")
    renderer = importlib.import_module("emotion_chip.render")
    monkeypatch.setattr(cli, "load_data", lambda *args: data)
    monkeypatch.setattr(renderer, "load_data", lambda *args: data)
    monkeypatch.setattr(sys, "stdin", io.StringIO("[sad] [laugh] Hi."))
    assert cli.main(["render","--dialect","fish_s2","--json","--no-sounds"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["text"] == "[sad] Hi." and out["params"] == {"speed":.92}
    assert out["dropped"][0]["reason"] == "sounds off"
    monkeypatch.setattr(sys, "stdin", io.StringIO("Hi."))
    assert cli.main(["render","--dialect","tagless"]) == 0
    assert capsys.readouterr().out == "Hi.\n"
    assert cli.main(["guide","--dialect","fish_s2","--model","test-model"]) == 0
    assert "Synthetic model note." in capsys.readouterr().out
    assert cli.main(["guide","--dialect","prose","--model","test-model","--skill"]) == 0
    assert capsys.readouterr().out.startswith("# Delivery score")
    monkeypatch.setattr(sys, "stdin", io.StringIO("[laugh] Hi."))
    assert cli.main(["render","--dialect","eleven_v4"]) == 0
    assert capsys.readouterr().out == "[laughs] Hi.\n"


def test_cli_module_validation(tmp_path):
    result = subprocess.run([sys.executable,"-m","emotion_chip","validate-data","--path",str(FIXTURES)],capture_output=True,text=True)
    assert result.returncode == 0 and result.stdout == "data valid\n"
    result = subprocess.run([sys.executable,"-m","emotion_chip","validate-data","--path",str(tmp_path)],capture_output=True,text=True)
    assert result.returncode == 1 and "Cannot load" in result.stderr


def test_golden_coverage():
    assert len(GOLDENS) >= 40
    for situation in ("failed-deploy", "criticism", "grief", "breakthrough", "safety"):
        assert sum(c["name"].startswith(situation+"-") for c in GOLDENS) == 3
    assert __version__ == "0.1.0.dev0"


def test_literal_cue_and_unmatched_non_crossing_span(data):
    out = render("[  Very SAD , curious  ] Hello.", "fish_s2", data=data)
    assert out.text == "[  Very SAD , curious  ] Hello."
    assert parse("[  Very SAD , curious  ]").sentences[0].tokens[0].words == ["sad", "curious"]
    score = parse("Hello [soft: unfinished")
    assert score.sentences[0].tokens == [Text("Hello "), Cue("soft", ["soft"], "plain", ["soft"]), Text(" unfinished")]
    assert [(x.kind,x.value,x.reason) for x in score.dropped] == [("span","soft","unterminated")]


def test_optional_emotion_arousal(tmp_path):
    root = fixture_copy(tmp_path)
    path = root / "emotions.yaml"
    rows = yaml.safe_load(path.read_text())
    rows[0].pop("arousal_bucket")
    path.write_text(yaml.safe_dump(rows))
    data = load_data(root)
    assert render("[curious] Hi.", "fish_s2", data=data).params == {"speed":1.03}


# Review fixes (orchestrator review of the M0 core, 2026-10-22).

def test_sound_inside_span_splits_the_span(data):
    text = "[soft: I'm glad [laugh] you told me.]"
    assert render(text, "fish_drama3", data=data).text == "<soft>I'm glad</soft> [laughing] <soft>you told me.</soft>"
    for dialect in DIALECTS:
        out = render(text, dialect, data=data).text
        assert "soft:" not in out and "glad" in out and "told me" in out


@pytest.mark.parametrize("dialect", ["fish_s2", "fish_drama3", "eleven_v4"])
def test_crying_sobbing_and_screaming_are_spoken_as_written(dialect, data):
    for cue_text in ("very hysterical", "crying softly", "slightly sobbing", "Screams", "sob", "wailing"):
        out = render(f"[{cue_text}] Okay.", dialect, data=data)
        assert out.text == f"[{cue_text}] Okay." and out.dropped == [], (dialect, cue_text)
    out = render("[[register: sobbing]] Hello.", dialect, data=data)
    assert out.text == "[sobbing] Hello." and not [d for d in out.dropped if d.kind == "header"]


def test_vendor_sound_spellings_are_events(data):
    score = parse("[laughs] [Sighing] [long pause] Yes.")
    assert [t.id for s in score.sentences for t in s.tokens if isinstance(t, Event)] == ["laugh", "sigh", "pause:long"]
    out = render("[laughs] [sighs] Yes.", "eleven_v4", data=data)
    assert out.text == "[laughs] [sighs] Yes." and out.dropped == []
    assert render("[laughing] I see.", "fish_s2", {"sounds": False}, data).text == "I see."


def test_hysterical_sets_params_like_any_other_cue():
    out = render("[hysterical] Hi.", "fish_s2", data=load_data())
    assert out.text == "[hysterical] Hi." and out.group == "panic_fear"


def test_plan_segments_carry_their_own_group(data):
    out = render("[sad] One. [very curious] Two. Three.", "fish_s2", data=data)
    assert [s.group for s in out.plan.segments] == [out.group, "interest_curiosity", "interest_curiosity"]
    assert [s.rung for s in out.plan.segments] == ["plain", "very", "very"]
    assert all(s.params == out.params for s in out.plan.segments)


def test_guide_shows_score_forms_not_vendor_syntax(data):
    for dialect in ("fish_s2", "fish_drama3", "eleven_v4"):
        section = build_prompt_section(dialect, "test-model", data)
        for vendor in ("{text}", "→", "<soft>", "[softly]", "[soft tone]", "uppercase"):
            assert vendor not in section, (dialect, vendor)


# Cross-model review fixes (Codex review of beda4bd, 2026-10-22).

@pytest.mark.parametrize("text", [
    "[soft: a [slow: b [laugh] c] d]",
    "[soft: a [slow: b [stress: c [laugh] d] e] f]",
    "[soft: a [slow: b. [laugh] c] d]",
    "[soft: a [happy [laugh]] b]",
    "[whisper: one [soft: two [sigh] three] four.] Five.",
])
def test_no_operator_word_is_spoken_at_any_depth(text, data):
    ops = re.compile(r"\b(?:soft|stress|slow|fast|whisper|smiling|emphasis|forceful):")
    for dialect in DIALECTS:
        out = render(text, dialect, data=data).text
        assert not ops.search(out), (dialect, out)
        for piece in re.compile(r"(?<=[.!?])(?:\s|\n)|(?:\n\n)").split(text):
            assert not ops.search(render(piece, dialect, data=data).text)
    assert render("[soft: a [slow: b [laugh] c] d]", "fish_drama3", data=data).text == \
        "<soft>a</soft> <slow>b</slow> [laughing] <slow>c</slow> <soft>d</soft>"


@pytest.mark.parametrize("text, expected", [
    ("[soft: [hap[laugh]py] a]", "[hap py] [laughing] <soft>a</soft>"),
    ("[happy [laugh]] Hi.", "[happy] [laughing] Hi."),
    ("[slow: ] Hi.", "Hi."),
    ("Hi [ there", "Hi there"),
])
def test_malformed_nesting_never_fuses_or_leaks(text, expected, data):
    assert render(text, "fish_drama3", data=data).text == expected


def test_fuzz_authorship_and_no_operator_leak(data):
    """Every rendered cue word appears in the agent's raw text, and no operator word is spoken."""
    import random
    rng = random.Random(20261022)
    atoms = ["[soft: ", "[slow: ", "[laugh]", "]", "[happy ", "[sad]", ". ", "a", "hap", "py", " ", "[", "[[register: x]]"]
    ops = re.compile(r"\b(?:soft|stress|slow|fast|whisper|smiling|emphasis|forceful):")
    for _ in range(3000):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 14)))
        score = parse(text)
        for dialect in DIALECTS:
            out = render(score, dialect, data=data)
            assert not ops.search(out.text), (text, dialect, out.text)
            if dialect in ("fish_s2", "fish_drama3", "eleven_v4"):
                assert check(score, out, data) == [], (text, dialect, out.text)


def test_override_never_borrows_a_later_rung(data):
    out = render("One. [very curious] Two. Three.", "fish_s2", {"params_from": "sadness"}, data)
    assert [(s.group, s.rung) for s in out.plan.segments] == [
        ("sadness", "plain"), ("interest_curiosity", "very"), ("interest_curiosity", "very")]


def test_register_header_sets_params_segment_and_opening_cue(data):
    out = render("[[register: sad]] Hello. Still here.", "fish_s2", data=data)
    assert out.text == "[sad] Hello. Still here."
    assert out.group == "sadness" and out.params == {"speed": 0.92}
    assert [s.group for s in out.plan.segments] == ["sadness", "sadness"]
    assert not [d for d in out.dropped if d.kind == "header"]
    assert check(parse("[[register: sad]] Hello."), out, data) == []


def test_register_header_with_a_leading_sentence_cue(data):
    out = render("[[register: sad]] [happy] Good. Fine.", "fish_s2", data=data)
    assert out.text == "[happy] Good. Fine."
    assert out.params == {"speed": 0.92}
    assert [s.group for s in out.plan.segments] == ["happiness", "happiness"]
    assert any("register header" in note for note in out.notes)


def test_register_header_on_tagless(data):
    out = render("[[register: sad]] Hello.", "tagless", data=data)
    assert out.text == "Hello." and [d.kind for d in out.dropped] == ["header"]


def test_parse_uses_the_active_data_events(tmp_path):
    root = fixture_copy(tmp_path)
    path = root / "sounds.yaml"
    sounds = yaml.safe_load(path.read_text())
    sounds["events"]["hum"] = {"kind": "affect"}
    path.write_text(yaml.safe_dump(sounds, sort_keys=False))
    local = load_data(root)
    assert isinstance(parse("[hum] a.", local).sentences[0].tokens[0], Event)
    assert not isinstance(parse("[hum] a.").sentences[0].tokens[0], Event)
    out = render("[hum] a. [laugh] b.", "fish_s2", {"sounds": False}, local)
    assert [(d.value, d.reason) for d in out.dropped] == [("hum", "sounds off"), ("laugh", "sounds off")]


@pytest.mark.parametrize("value,expected", [(2, 1.0), (-1, 0.0), (0.6, 0.6)])
def test_eleven_similarity_is_clamped(value, expected, data):
    assert render("Hi.", "eleven_v4", {"eleven_similarity": value}, data).params["similarity_boost"] == expected


@pytest.mark.parametrize("template", ["{missing}", "{text} {0}", "{text", "plain", "{text:Q}", "{text}{text}", "{text!r}"])
def test_span_templates_must_use_only_text(tmp_path, template):
    root = fixture_copy(tmp_path)
    path = root / "dialects" / "fish_s2.yaml"
    table = yaml.safe_load(path.read_text())
    table["spans"]["soft"] = template
    path.write_text(yaml.safe_dump(table, sort_keys=False))
    with pytest.raises(ValueError, match="placeholder"):
        load_data(root)


def _damaged(tmp_path, name, edit):
    root = fixture_copy(tmp_path)
    path = root / name
    value = yaml.safe_load(path.read_text())
    edit(value)
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return root


@pytest.mark.parametrize("name,edit,match", [
    ("sounds.yaml", lambda v: v.setdefault("aliases", {}).update(purrs="purr"), "not an event"),
    ("dialects/eleven_v4.yaml", lambda v: v.update(variants={"eleven_v4": "big"}), "variants"),
    ("dialects/eleven_v4.yaml", lambda v: v.pop("variants"), "variants"),
    ("dialects/fish_s2.yaml", lambda v: v["events"].update(boop="[boop]"), "not a declared sound event"),
    ("dialects/fish_s2.yaml", lambda v: v["spans"].update(soft="very {text}"), "unmarked text"),
    ("dialects/eleven_v4.yaml", lambda v: v["events"].update(laugh="[laughs] haha"), "vendor literals only"),
    ("dialects/fish_drama3.yaml", lambda v: (v.update(speaker="<|speaker:{id!r}|>"),
                                             v["phrases"].append("<|speaker:{id!r}|>")), "placeholder"),
    ("dialects/fish_drama3.yaml", lambda v: (v.update(speaker="<|speaker:{name}|>"),
                                             v["phrases"].append("<|speaker:{name}|>")), "placeholder"),
    ("sounds.yaml", lambda v: v.setdefault("aliases", {}).update(laugh="sigh"), "override"),
    ("dialects/fish_s2.yaml", lambda v: v["events"].update(laugh="uppercase"), "vendor literals only"),
    ("dialects/fish_s2.yaml", lambda v: v["events"].update(laugh="{text}"), "vendor literals only"),
    ("dialects/fish_drama3.yaml", lambda v: (v.update(speaker="sad {id}"), v["phrases"].append("sad {id}")), "marker"),
    ("dialects/fish_s2.yaml", lambda v: v["spans"].update(robot="[soft tone] {text}"), "score grammar"),
    ("dialects/fish_s2.yaml", lambda v: (v["spans"].update(soft="[angry] {text}"), v["phrases"].append("[angry]"),
                                         v["inverse"].update({"[angry]": "angry"})), "is an emotion cue"),
    ("dialects/fish_s2.yaml", lambda v: (v["events"].update(laugh="[very happy]"), v["phrases"].append("[very happy]"),
                                         v["inverse"].update({"[very happy]": "laugh"})), "is an emotion cue"),
    ("sounds.yaml", lambda v: v.setdefault("aliases", {}).update(happy="laugh"), "emotion cue or score form"),
    ("sounds.yaml", lambda v: v.setdefault("aliases", {}).update({"speaker: 1": "laugh"}), "emotion cue or score form"),
    ("sounds.yaml", lambda v: v["events"].update(soft={"kind": "affect"}), "emotion cue or score form"),
])
def test_sound_and_variant_tables_are_validated(tmp_path, name, edit, match):
    with pytest.raises(ValueError, match=match):
        load_data(_damaged(tmp_path, name, edit))


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    root = fixture_copy(tmp_path)
    path = root / "groups.yaml"
    path.write_text(path.read_text() + "\n" + path.read_text().split("\n", 1)[0] + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_data(root)


@pytest.mark.parametrize("text,dialect,expected", [
    ("hap[laugh]py.", "tagless", "hap py."),
    ("foo[pause]bar.", "tagless", "foo bar."),
    ("one[breath]two.", "fish_s2", "one two."),
    ("[speaker: 1]Hi.", "fish_drama3", "Hi."),
    ("a[whisper: b]c.", "prose", "a b c."),
])
def test_dropped_tokens_never_fuse_words(data, text, dialect, expected):
    out = render(text, dialect, data=data)
    assert out.text == expected and out.plan.segments[0].text == expected


def test_segment_text_keeps_word_boundaries_at_kept_tokens(data):
    out = render("ha[laugh]ha.", "fish_s2", data=data)
    assert out.text == "ha[laughing]ha." and out.plan.segments[0].text == "ha ha."


@pytest.mark.parametrize("quoted", ['He said "go."', "He said 'go.'", "He said “go.”", "(It went.)"])
def test_sentence_ends_before_a_closing_quote(quoted, data):
    score = parse(f"[breath] One. {quoted} [exhale] Three.", data)
    assert len(score.sentences) == 3
    assert apply_policy(score, data)[1] == []


@pytest.mark.parametrize("text, count", [
    ("[breath] One. [soft: Two.] [exhale] Three.", 3),
    ('[soft: He said "go."] Then.', 2),
    ("[soft: Ask Dr.] Smith.", 1),
    ("[soft: Two.][exhale] Three.", 1),
])
def test_a_span_can_end_its_sentence(data, text, count):
    score = parse(text, data)
    assert len(score.sentences) == count
    assert apply_policy(score, data)[1] == []


@pytest.mark.parametrize("text, expected", [("[[laughing]] Hi.", "Hi."), ("[[register: happy, laughing]] Hi.", "[happy] Hi."),
                                            ("[[register: Laughs]] [sad] Hi.", "[sad] Hi.")])
def test_sounds_off_drops_a_sound_named_in_the_header(text, expected):
    data = load_data()
    assert render(text, "fish_s2", {"sounds": False}, data).text == expected
    on = render(text, "fish_s2", data=data)
    assert on.dropped == [] or all(d.reason != "sounds off" for d in on.dropped)


def test_breaths_and_sounds_are_never_capped(data):
    text = "[breath] One. [exhale] Two. [breath:shaky] Three. [laugh] [laugh] [sigh] [gasp] Four."
    assert apply_policy(parse(text), data)[1] == []
    assert apply_policy(parse(text), data, Options(sounds=False))[1] == [
        Dropped("event", e, "sounds off") for e in ("laugh", "laugh", "sigh", "gasp")]


@pytest.mark.parametrize("text", ["[speaker: hello there] Hi.", "[speaker: 1|><soft>] Hi.", "[speaker: ] Hi."])
def test_speaker_ids_are_numeric_only(data, text):
    score = parse(text, data)
    assert not any(isinstance(t, Speaker) for s in score.sentences for t in s.tokens)
    assert [d.reason for d in score.dropped] == ["invalid id"]
    assert render(text, "fish_drama3", Options(allow_speakers=True), data).text == "Hi."
    assert render("[speaker: 2] Hi.", "fish_drama3", Options(allow_speakers=True), data).text == "<|speaker:2|> Hi."


def test_titles_do_not_end_sentences(data):
    score = parse("[breath] Dr. Smith arrived. [exhale] Then e.g. this. Mr. Lee left.", data)
    assert len(score.sentences) == 3


@pytest.mark.parametrize("text", ["[" * 20000, "[[" * 10000, "[soft: " + "[" * 10000 + "]", "a [" * 10000],
                         ids=["openers", "doubles", "span", "words"])
def test_unmatched_brackets_parse_in_linear_time(data, text):
    import time
    start = time.perf_counter()
    parse(text, data)
    assert time.perf_counter() - start < 2


def test_model_notes_are_validated(tmp_path):
    root = fixture_copy(tmp_path)
    (root / "models").mkdir(exist_ok=True)
    (root / "models" / "broken.yaml").write_text("- not\n- a mapping\n")
    with pytest.raises(ValueError, match="model note broken"):
        load_data(root)
    (root / "models" / "broken.yaml").write_text("note: fine\n")
    data = load_data(root)
    (root / "models" / "broken.yaml").write_text("note: [1, 2]\n")
    with pytest.raises(ValueError, match="model note broken"):
        build_prompt_section("fish_s2", "broken", data)


def test_streamed_pieces_keep_neutral_params(data):
    full = render("[mystery] One. [sad] Two.", "fish_s2", data=data)
    assert full.group is None and full.params == {"speed": 1.0}
    for piece in ("[mystery] One.", "[sad] Two."):
        assert render(piece, "fish_s2", {"params_from": full.params}, data).params == full.params


@pytest.mark.parametrize("dialect", DIALECTS)
def test_ordinary_brackets_stay_text(dialect, data):
    out = render("See [documentation](https://example.com) now. Use items[0], see [1] and http://[::1]/ here.",
                 dialect, data=data)
    assert "See documentation (https://example.com) now." in out.text
    assert "items[0], see [1] and http://[::1]/ here." in out.text
    assert not [d for d in out.dropped if d.kind == "cue"]
    assert check(parse("Use items[0] and [1].", data), render("Use items[0] and [1].", dialect, data=data), data) == []


def test_a_paren_word_after_a_cue_is_not_a_link(data):
    tokens = parse("Ok. [sad](sobbing) Hi.", data).sentences[1].tokens
    assert isinstance(tokens[0], Cue) and tokens[0].raw == "sad" and tokens[1].s.startswith("(sobbing)")


def test_params_dict_override_reports_no_cue_group(data):
    full = render("[sad] One. Two.", "fish_s2", data=data)
    piece = render("[happy] Two.", "fish_s2", {"params_from": full.params}, data)
    assert piece.params == full.params and piece.group is None
    assert render("Two.", "fish_s2", {"params_from": full.group}, data).group == full.group


def test_sound_names_match_across_whitespace(data):
    for text in ("[long  pause] Hi.", "[long\npause] Hi.", "[Laughs \t harder] Hi."):
        token = parse(text, data).sentences[0].tokens[0]
        assert isinstance(token, Event), text


def test_guide_offers_the_whole_range_with_no_caps():
    for text in (build_prompt_section("fish_s2", "absent", load_data()), build_skill("fish_s2", "absent", load_data())):
        for limit in ("at most one of the sounds", "extra ones are dropped", "No whisper", "never playful",
                      "human resource", "use discretion", "At most one breath", "at most two"):
            assert limit not in text, limit
    assert "crying, sobbing, screaming and whispering" in build_prompt_section("fish_s2", "absent", load_data())


def test_misplaced_header_and_removed_cues_keep_boundaries(data):
    assert render("hap[[register: sad]]py.", "fish_s2", data=data).text == "hap py."
    out = render("[[register: sad]] [pause] [happy] Hi.", "tagless", data=data)
    assert out.text == "Hi." and out.params == {}
    # A sound removed by the quiet switch leaves no extra header cue, and the header still sets params.
    out = render("[[register: sad]] [laugh] [happy] Hi.", "fish_s2", {"sounds": False}, data)
    assert out.text == "[happy] Hi." and out.params == {"speed": 0.92}


def test_cli_can_render_speakers(monkeypatch, capsys):
    from emotion_chip.cli import main
    monkeypatch.setattr(sys, "stdin", io.StringIO("[speaker: 1] Hi."))
    main(["render", "--dialect", "fish_drama3", "--allow-speakers"])
    assert "<|speaker:1|>" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("[speaker: 1] Hi."))
    main(["render", "--dialect", "fish_drama3"])
    assert capsys.readouterr().out.strip() == "Hi."
