"""Shared pgvector HNSW dimension constraints."""

from __future__ import annotations


SUPPORTED_HNSW_DIMENSIONS = frozenset({256, 512, 1024})


def hnsw_vector_type(dimensions: int) -> str:
    """Return a safe pgvector type for a supported HNSW dimension."""

    if dimensions not in SUPPORTED_HNSW_DIMENSIONS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_HNSW_DIMENSIONS))
        raise ValueError(
            f"PostgreSQL HNSW only supports configured dimensions: {supported}"
        )
    return f"vector({dimensions})"
