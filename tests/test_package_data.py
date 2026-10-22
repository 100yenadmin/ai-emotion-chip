"""Authoring parity and an isolated dependency-free runtime."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from emotion_chip.cli import main
from emotion_chip.data import DATA_ROOT, build_bundle, check_bundle, load_data


def test_package_data(runtime_data):
    authored = load_data(DATA_ROOT)
    for key in ("emotions", "aliases", "groups", "sounds", "dialects", "guide", "models"):
        assert getattr(runtime_data, key) == getattr(authored, key)
    check_bundle()
    bundle = json.loads((DATA_ROOT / "bundle.json").read_text())
    assert bundle["bundle_version"] == 1
    assert len(bundle["sources_sha256"]) == len(list(DATA_ROOT.rglob("*.yaml"))) + 3


@pytest.mark.parametrize("dialect", ["fish_s2", "fish_drama3", "eleven_v4", "tagless"])
def test_real_guides_offer_the_whole_range(dialect):
    from emotion_chip.guide import build_prompt_section, build_skill
    data = load_data(DATA_ROOT)
    for model in ("claude-opus-5-5", "claude-fable-5-1", "default"):
        for text in (build_prompt_section(dialect, model, data), build_skill(dialect, model, data)):
            assert "Hard moments" not in text and "human resource" not in text and "No whisper" not in text
            assert len(build_prompt_section(dialect, model, data)) <= 3800


@pytest.mark.parametrize("source", sorted(p.relative_to(DATA_ROOT).as_posix() for p in DATA_ROOT.rglob("*.yaml")))
def test_stale_bundle(tmp_path, source, capsys):
    root = tmp_path / "data"
    shutil.copytree(DATA_ROOT, root)
    with (root / source).open("a") as handle:
        handle.write("\n# authoring changed\n")
    assert main(["check-bundle", "--path", str(root)]) == 1
    assert "stale" in capsys.readouterr().err
    assert main(["build-bundle", "--path", str(root)]) == 0
    assert main(["check-bundle", "--path", str(root)]) == 0


def test_bundle_templates_are_sources(tmp_path):
    root = tmp_path / "data"
    shutil.copytree(DATA_ROOT, root)
    with (root / "guide" / "section.md").open("a") as handle:
        handle.write("\nEdited guide\n")
    with pytest.raises(ValueError, match="stale"):
        check_bundle(root)
    build_bundle(root)
    check_bundle(root)


def test_runtime_without_yaml(tmp_path):
    root = tmp_path / "runtime"
    shutil.copytree(DATA_ROOT.parent, root / "emotion_chip", ignore=shutil.ignore_patterns("__pycache__"))
    for path in (root / "emotion_chip" / "data").rglob("*"):
        if path.is_file() and path.name != "bundle.json":
            path.unlink()
    script = """
import sys
sys.modules['yaml'] = None
sys.path.insert(0, sys.argv[1])
from emotion_chip.render import render
from emotion_chip.data import load_data
from emotion_chip.guide import build_prompt_section, build_skill
from emotion_chip.hermes.registration import register_all
out = render('[slightly frustrated] Again. [sigh] [pause] Checking.', 'fish_s2')
assert out.text == '[slightly frustrated] Again. [sighing] [break] Checking.'
assert 'text_to_speech' in build_prompt_section('fish_s2', 'default', load_data())
assert 'text_to_speech' in build_skill('fish_drama3', 'default', load_data())
assert sys.modules['yaml'] is None
print('runtime without PyYAML ok')
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(root)],
                            cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runtime without PyYAML ok"
