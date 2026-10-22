import json
from pathlib import Path


def latest_release():
    versions = json.loads((Path(__file__).parent / "data" / "releases.json").read_text())
    return max(versions, key=lambda version: tuple(map(int, version.split("."))))
