"""Apply field weights to PostgreSQL repository full-text search."""

from __future__ import annotations

from alembic import op


revision = "0002_weighted_repository_fts"
down_revision = "0001_pgvector_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Rank symbols and headings above content and paths."""

    op.execute(
        """
        UPDATE repository_chunks
        SET search_document =
            setweight(to_tsvector('simple', coalesce(path, '')), 'C') ||
            setweight(to_tsvector('simple', coalesce(content, '')), 'B') ||
            setweight(
                to_tsvector(
                    'simple',
                    concat_ws(' ', coalesce(symbol, ''), coalesce(heading_path_json, ''))
                ),
                'A'
            )
        """
    )
    op.execute("ANALYZE repository_chunks")


def downgrade() -> None:
    """Restore uniform full-text-search weights."""

    op.execute(
        """
        UPDATE repository_chunks
        SET search_document = to_tsvector(
            'simple',
            concat_ws(
                E'\\n', path, coalesce(symbol, ''), content,
                coalesce(heading_path_json, '')
            )
        )
        """
    )
    op.execute("ANALYZE repository_chunks")
