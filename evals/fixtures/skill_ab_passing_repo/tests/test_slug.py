from src.slug import slugify


def test_slugify_normalizes_case_and_spaces() -> None:
    assert slugify("Repo Agent") == "repo-agent"
