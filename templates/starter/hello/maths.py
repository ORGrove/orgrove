"""Small arithmetic helpers."""


def add(a: float, b: float) -> float:
    return a + b


def mean(values: list[float]) -> float:
    """The average of values. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("mean of an empty list")
    return sum(values) / len(values)
