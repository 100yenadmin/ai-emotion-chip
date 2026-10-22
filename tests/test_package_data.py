"""The other lane owns real research data; validate it only when present."""
from pathlib import Path
import pytest
from emotion_chip.data import load_data

ROOT = Path(__file__).parents[1] / "emotion_chip" / "data"


@pytest.mark.skipif(not (Path(__file__).parents[1] / "emotion_chip" / "data" / "emotions.yaml").exists(),
                    reason="real emotions.yaml is owned by the data lane and not present")
def test_package_data():
    load_data()


@pytest.mark.skipif(not (ROOT / "emotions.yaml").exists(), reason="real emotion data not present")
@pytest.mark.parametrize("dialect", ["fish_s2", "fish_drama3", "eleven_v4", "tagless"])
def test_real_guides_offer_the_whole_range(dialect):
    from emotion_chip.guide import build_prompt_section, build_skill
    data = load_data(ROOT)
    for model in ("claude-opus-5-5", "claude-fable-5-1", "default"):
        for text in (build_prompt_section(dialect, model, data), build_skill(dialect, model, data)):
            assert "Hard moments" not in text and "human resource" not in text and "No whisper" not in text
            assert len(build_prompt_section(dialect, model, data)) <= 3800
