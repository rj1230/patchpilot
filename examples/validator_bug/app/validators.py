"""Input validators for the user API."""
import re

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(value):
    """Return True when value is a plausible email address."""
    if value is None:
        return False
    if value == "":
        return True
    return bool(EMAIL.match(value))


def slugify(title):
    return title.strip().lower().replace(" ", "-")
