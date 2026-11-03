import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.persistence.database import make_engine
from app.persistence.repository import CallRepository
from app.seed import BUSINESS_ID, seed
from app.services.orchestrator import ConversationOrchestrator

BACKEND = Path(__file__).resolve().parents[2]


def test_sqlite_migrations_upgrade_downgrade_and_metadata_match(engine):
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["database_url"] = str(engine.url)
    command.check(config)
    assert "policy_decisions" in inspect(engine).get_table_names()
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
    assert "model_invocations" in inspect(engine).get_table_names()


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not set")
def test_postgres_migration_and_end_to_end_roundtrip(providers, telemetry):
    """Only a unique temporary schema is touched, never existing application tables."""
    engine = make_engine(os.environ["TEST_POSTGRES_URL"])
    schema = f"pa_test_{uuid4().hex}"
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["database_url"] = str(engine.url)
    with engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        connection.commit()
        config.attributes["connection"] = connection
        try:
            command.upgrade(config, "head")
            with Session(connection, expire_on_commit=False) as session:
                seed(session)
                repo = CallRepository(session)
                result = ConversationOrchestrator(repo, providers, telemetry).run(
                    BUSINESS_ID, "MRF 195/65 R15 ka rate kya hai?"
                )
                detail = repo.detail(result.call_id)
                assert result.outcome == "RESOLVED"
                assert len(detail["messages"]) == 3 and detail["policy_decisions"]
                assert detail["started_at"].tzinfo is not None
                assert detail["outcome"]["outcome"] == "RESOLVED"
                assert len(detail["model_invocations"]) == 2
            command.downgrade(config, "base")
            assert inspect(connection).get_table_names() == ["alembic_version"]
        finally:
            connection.rollback()
            connection.exec_driver_sql("SET search_path TO public")
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            connection.commit()
    engine.dispose()
