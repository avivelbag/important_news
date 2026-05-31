"""Database engine, session factory, and schema initialisation for stories.db."""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session

from src.models import Base

_DEFAULT_DB_PATH = Path("data/stories.db")


def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
    """Activate foreign-key enforcement for every new SQLite connection.

    SQLite ships with FK constraints disabled by default; this pragma turns
    them on so that referential integrity is actually enforced at runtime.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(db_url: str | None = None) -> Engine:
    """Return a SQLAlchemy engine.

    Uses a file-backed SQLite database at *data/stories.db* by default.
    Pass *db_url* to override (e.g. ``"sqlite://"`` for an in-memory DB in tests).
    """
    if db_url is None:
        _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{_DEFAULT_DB_PATH}"
    engine = create_engine(db_url, echo=False)
    if db_url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_fk)
    return engine


def init_db(engine: Engine | None = None) -> None:
    """Create all ORM-defined tables if they do not already exist.

    Idempotent — safe to call on every application start.
    """
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)


def get_session(engine: Engine | None = None) -> Session:
    """Return a new *Session* bound to *engine* (or the default engine).

    The caller is responsible for committing and closing the session.
    """
    if engine is None:
        engine = get_engine()
    return Session(engine)


if __name__ == "__main__":
    engine = get_engine()
    init_db(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ).fetchall()
    print([row[0] for row in rows])
