from repo_agent.rag.index import build_fts_or_query


def test_build_fts_or_query_uses_disjunction_for_natural_language() -> None:
    assert build_fts_or_query("Where does QuerySet filter rows?") == (
        '"queryset" OR "filter" OR "rows" OR "query" OR "set"'
    )


def test_build_fts_or_query_removes_symbol_lookup_scaffolding() -> None:
    assert build_fts_or_query(
        "Where is the Django class AppConfig implemented?"
    ) == '"appconfig" OR "app" OR "config"'


def test_build_fts_or_query_splits_cjk_bigrams() -> None:
    query = build_fts_or_query("代码检索")
    assert query is not None
    assert '"代码检索"' in query
    assert '"代码"' in query
    assert '"检索"' in query
