"""Engine creation and factories for independent request/transaction-owned database sessions."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(url: str):
    """Create a SQLAlchemy engine with hidden SQL parameters and SQLite foreign keys enabled.
    Permit SQLite worker-thread access; in-memory SQLite uses StaticPool so sessions see one
    database. The app or CLI owns engine disposal; PostgreSQL uses its normal pooled engine.
    """
    options = {}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            options["poolclass"] = StaticPool
    engine = create_engine(url, hide_parameters=True, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            """Enable foreign-key enforcement on every new SQLite connection. This mirrors
            PostgreSQL constraints instead of allowing tests to pass with broken references.
            """
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def make_sessions(engine):
    """Return a session factory with expire_on_commit=False for the supplied engine. Each
    request/worker transaction owns a separate Session; keeping loaded scalar/domain values
    accessible after commit does not make sessions thread-safe.
    """
    return sessionmaker(engine, expire_on_commit=False)
