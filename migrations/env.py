"""Alembic 环境入口。"""

from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _url() -> str:
    value = os.environ.get("REPO_AGENT_POSTGRES_DSN")
    resolved = value or config.get_main_option("sqlalchemy.url")
    if resolved.startswith("postgresql://"):
        return resolved.replace("postgresql://", "postgresql+psycopg://", 1)
    return resolved


def run_migrations_offline() -> None:
    """离线输出 SQL。"""

    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线执行迁移。"""

    from sqlalchemy import create_engine

    connectable = create_engine(_url(), pool_pre_ping=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
