from checksum import checksum


def test_empty():
    assert checksum(b"") == 0


def test_standard_check():
    assert checksum(b"123456789") == 0xCBF43926


def test_hello():
    assert checksum(b"hello") == 0x3610A686


def test_quick_brown_fox():
    assert checksum(b"The quick brown fox jumps over the lazy dog") == 0x414FA339


def test_required_value():
    assert checksum(b"payload") == 0x422C6A14
