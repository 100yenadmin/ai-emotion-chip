from pathlib import Path
import tempfile

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
def bench_temp_root(monkeypatch):
    """The benches refuse /tmp for results and agent workspaces. Where the platform's temp folder is under /tmp
    (Linux), a test that writes there moves the guards' /tmp root aside; the refusals are tested with real paths."""
    from bench import items
    from bench.e4 import run
    temp = Path(tempfile.gettempdir()).resolve()
    if temp == items.TMP or items.TMP in temp.parents:
        for module in (items, run):
            monkeypatch.setattr(module, "TMP", Path("/nonexistent-temp-root"))


@pytest.fixture
def runtime_data():
    """M1 consumes the shipped JSON bundle, rather than YAML fixtures."""
    return load_data()
