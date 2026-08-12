def build_url(base_url: str, resource: str) -> str:
    return f"{base_url.rstrip('/')}/{resource.lstrip('/')}"
