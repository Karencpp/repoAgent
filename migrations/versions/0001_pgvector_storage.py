"""创建 RepoAgent PostgreSQL/pgvector 持久化表。"""

from __future__ import annotations

from alembic import op


revision = "0001_pgvector_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 schema、索引和约束。"""

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_index_state (
            project_id TEXT PRIMARY KEY,
            repo_revision TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_files (
            project_id TEXT NOT NULL,
            repo_revision TEXT NOT NULL,
            path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, path)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS repository_chunks (
            chunk_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            repo_revision TEXT NOT NULL,
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL CHECK (start_line > 0),
            end_line INTEGER NOT NULL CHECK (end_line >= start_line),
            kind TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'python',
            symbol TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
            embedding vector,
            search_document tsvector NOT NULL,
            token_text TEXT NOT NULL DEFAULT '',
            heading_path_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_repository_chunks_project_revision ON repository_chunks(project_id, repo_revision)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_repository_chunks_project_path ON repository_chunks(project_id, path)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_repository_chunks_fts ON repository_chunks USING gin(search_document)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_repository_chunks_trgm ON repository_chunks USING gin(token_text gin_trgm_ops)")
    for dimensions in (256, 512, 1024):
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_repository_chunks_embedding_hnsw_{dimensions}
            ON repository_chunks
            USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops)
            WHERE embedding_dimensions = {dimensions}
            """
        )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            memory_key TEXT,
            memory_type TEXT NOT NULL CHECK (memory_type IN ('episodic','semantic','perceptual')),
            content TEXT NOT NULL,
            claim_status TEXT NOT NULL CHECK (claim_status IN ('hypothesis','verified','refuted')),
            importance DOUBLE PRECISION NOT NULL CHECK (importance >= 0 AND importance <= 1),
            scope TEXT NOT NULL CHECK (scope IN ('project','revision')),
            repo_revision TEXT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK (status IN ('active','superseded','forgotten','expired')),
            supersedes_memory_id TEXT,
            expires_at TIMESTAMPTZ,
            embedding vector,
            embedding_model TEXT NOT NULL,
            embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
            search_document tsvector NOT NULL,
            token_text TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_key ON memories(project_id, memory_key) WHERE status = 'active' AND memory_key IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_status ON memories(project_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_memories_filters ON memories(project_id, memory_type, claim_status, status, importance)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING gin(search_document)")
    for dimensions in (256, 512, 1024):
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw_{dimensions}
            ON memories
            USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops)
            WHERE embedding_dimensions = {dimensions}
            """
        )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_evidence (
            memory_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            PRIMARY KEY(memory_id, evidence_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
            event_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_embedding_state (
            project_id TEXT PRIMARY KEY,
            embedding_model TEXT NOT NULL,
            embedding_dimensions INTEGER NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_curation_decisions (
            project_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            candidate_json TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            matched_memory_id TEXT,
            result_memory_id TEXT,
            decided_by TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (project_id, candidate_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
            consolidation_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            input_memory_ids_json TEXT NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            result_candidate_ids_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_proposals (
            proposal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            execution_key TEXT NOT NULL UNIQUE,
            proposal_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_attempts (
            attempt_id TEXT PRIMARY KEY,
            proposal_id TEXT,
            project_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            evaluation_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_events (
            event_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """删除 RepoAgent PostgreSQL 持久化表。"""

    for table in (
        "run_events",
        "maintenance_attempts",
        "maintenance_proposals",
        "memory_consolidation_runs",
        "memory_curation_decisions",
        "memory_embedding_state",
        "memory_lifecycle_events",
        "memory_evidence",
        "memories",
        "repository_chunks",
        "repository_files",
        "repository_index_state",
        "projects",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
