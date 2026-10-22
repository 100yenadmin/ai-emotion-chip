from pathlib import Path
import pytest
import yaml

from emotion_chip.data import load_data

FIXTURES = Path(__file__).parent / "fixtures" / "data"
GOLDENS = yaml.safe_load((Path(__file__).parent / "goldens" / "core.yaml").read_text())


@pytest.fixture(scope="session")
def data():
    return load_data(FIXTURES)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))


@pytest.fixture
def runtime_data():
    """M1 consumes the shipped JSON bundle, rather than YAML fixtures."""
    return load_data()
