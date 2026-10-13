from app.validators import slugify, validate_email


def test_valid_email():
    assert validate_email("ada@example.com")


def test_missing_at_rejected():
    assert not validate_email("nope")


def test_slugify():
    assert slugify(" Hello World ") == "hello-world"
