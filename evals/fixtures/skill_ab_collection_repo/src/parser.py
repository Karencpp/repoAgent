def parse_line(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator:
        raise ValueError("line must contain '='")
    return key.strip(), item.strip()
