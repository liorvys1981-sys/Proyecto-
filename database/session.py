import os
from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base


@lru_cache
def get_engine():
    database_url = os.getenv("DATABASE_URL", "sqlite:///./yimi.db")
    options = {"pool_pre_ping": True}
    if database_url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool
    return create_engine(database_url, **options)


@lru_cache
def get_session_factory():
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def initialize_database() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('yimi_schema_initialization', 0))"
                )
            )
        Base.metadata.create_all(connection)
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("usage_records")
        }
        new_columns = {
            "input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "provider_cost_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "input_provider_cost_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "output_provider_cost_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "input_sell_price_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "output_sell_price_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "sell_price_micro_usd": "INTEGER NOT NULL DEFAULT 0",
            "markup_bps": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, definition in new_columns.items():
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE usage_records ADD COLUMN {column_name} {definition}"
                    )
                )
        run_columns = {
            column["name"] for column in inspect(connection).get_columns("agent_runs")
        }
        if "approval_required" not in run_columns:
            connection.execute(
                text(
                    "ALTER TABLE agent_runs ADD COLUMN approval_required "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "processing_started_at" not in run_columns:
            connection.execute(
                text("ALTER TABLE agent_runs ADD COLUMN processing_started_at TIMESTAMP")
            )
        if "idempotency_key" not in run_columns:
            connection.execute(
                text("ALTER TABLE agent_runs ADD COLUMN idempotency_key VARCHAR(128)")
            )
        if "request_hash" not in run_columns:
            connection.execute(
                text("ALTER TABLE agent_runs ADD COLUMN request_hash VARCHAR(64)")
            )
        if "finished_at" not in run_columns:
            connection.execute(
                text("ALTER TABLE agent_runs ADD COLUMN finished_at TIMESTAMP")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_agent_runs_tenant_idempotency_key "
                "ON agent_runs (tenant_id, idempotency_key)"
            )
        )


def get_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
