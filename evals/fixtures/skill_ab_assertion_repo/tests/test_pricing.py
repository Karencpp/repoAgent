from src.pricing import discounted_total


def test_discounted_total_applies_discount() -> None:
    assert discounted_total(100, 15) == 85
