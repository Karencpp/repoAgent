from src.parser import parse_line


@pytest.mark.unit
def test_parse_line_rejects_missing_separator() -> None:
    with pytest.raises(ValueError):
        parse_line("missing separator")
