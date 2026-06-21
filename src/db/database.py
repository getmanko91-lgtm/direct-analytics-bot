from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'app.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from src.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def _migrate_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "clients" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("clients")}
    with engine.begin() as conn:
        if "monthly_budget" not in columns:
            conn.execute(text("ALTER TABLE clients ADD COLUMN monthly_budget FLOAT NOT NULL DEFAULT 0"))
        if "directologist" not in columns:
            conn.execute(text("ALTER TABLE clients ADD COLUMN directologist VARCHAR(32) NOT NULL DEFAULT 'Ксюша'"))
        if "max_chat_id" not in columns:
            conn.execute(text("ALTER TABLE clients ADD COLUMN max_chat_id VARCHAR(64) NOT NULL DEFAULT ''"))
