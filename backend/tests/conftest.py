import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability.telemetry import Telemetry, logger
from app.persistence.database import make_engine, make_sessions
from app.persistence.repository import CallRepository
from app.providers.mock import build_providers
from app.seed import seed
from app.services.orchestrator import ConversationOrchestrator

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    """Keep ordinary tests independent of developer credentials and local voice mode.

    Tests inject their own settings or fake transports; only the explicitly invoked
    live utility may consume the developer's .env. PostgreSQL's test URL is retained.
    """
    import os

    from app.config import Settings
    from app.realtime.config import VoiceSettings

    for cls in (Settings, VoiceSettings):
        monkeypatch.setitem(cls.model_config, "env_file", None)
    for name in list(os.environ):
        if name.startswith("VOICE_") or name in {
            "API_KEY",
            "PROVIDER_MODE",
            "DATABASE_URL",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        }:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(BACKEND / "alembic.ini"))
    command.upgrade(config, "head")
    db_engine = make_engine(url)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def session(engine):
    with make_sessions(engine)() as db:
        seed(db)
        yield db


@pytest.fixture
def spans():
    return InMemorySpanExporter()


@pytest.fixture
def telemetry(spans):
    value = Telemetry(span_exporter=spans)
    yield value
    value.shutdown()


@pytest.fixture
def providers():
    return build_providers()


@pytest.fixture
def runner(session, providers, telemetry):
    return ConversationOrchestrator(CallRepository(session), providers, telemetry)


@pytest.fixture
def events():
    records = []

    class Capture(logging.Handler):
        """Collect structured application payloads in tests without changing production
        formatting. The fixture owns handler installation/removal and logger-level
        restoration.
        """

        def emit(self, record):
            """Append only the application's structured payload so tests can assert correlation
            and absence of sensitive text.
            """
            records.append(record.payload)

    capture = Capture()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    yield records
    logger.removeHandler(capture)
    logger.setLevel(previous_level)
