import definitely_missing_skill_ab_dependency

from src.client import build_url


def test_build_url_normalizes_slashes() -> None:
    assert build_url("https://example.test/", "/users") == "https://example.test/users"
