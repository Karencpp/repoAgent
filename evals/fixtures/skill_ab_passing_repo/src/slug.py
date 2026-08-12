def slugify(value: str) -> str:
    return "-".join(value.casefold().split())
