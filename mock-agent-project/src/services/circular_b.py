"""Other half of the circular import pair. See circular_a.py."""


def call_from_b():
    from src.services.circular_a import from_a
    return from_a()


def from_b():
    return "b"
