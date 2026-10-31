"""Greetings."""


def greet(name: str) -> str:
    """Return a greeting for name; an empty name greets the world."""
    name = name.strip()
    return f"Hello, {name or 'world'}!"
