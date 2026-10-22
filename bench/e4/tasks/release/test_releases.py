from releases import latest_release


def test_version_format():
    parts = latest_release().split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts)


def test_not_lexicographic():
    assert tuple(map(int, latest_release().split("."))) >= (1, 10, 0)


def test_required_release():
    assert latest_release() == "2.0.0"
