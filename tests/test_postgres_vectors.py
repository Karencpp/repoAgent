from __future__ import annotations

import pytest

from repo_agent.postgres_vectors import hnsw_vector_type


@pytest.mark.parametrize("dimensions", (256, 512, 1024))
def test_hnsw_vector_type_accepts_configured_dimensions(dimensions: int) -> None:
    assert hnsw_vector_type(dimensions) == f"vector({dimensions})"


@pytest.mark.parametrize("dimensions", (0, 32, 64, 1536, 2048, 4096))
def test_hnsw_vector_type_rejects_unindexed_dimensions(dimensions: int) -> None:
    with pytest.raises(ValueError, match="PostgreSQL HNSW"):
        hnsw_vector_type(dimensions)
