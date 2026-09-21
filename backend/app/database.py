"""Database setup for DoseKeep's local operational records."""

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = os.getenv("DOSEKEEP_DATABASE_URL", "sqlite:////data/dosekeep.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def ensure_schema() -> None:
    """Apply the one additive migration required by the initial MVP."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(packs)"))}
        if "quantity_in_dosette" not in columns:
            connection.execute(text("ALTER TABLE packs ADD COLUMN quantity_in_dosette INTEGER NOT NULL DEFAULT 0"))
        if "user_id" not in columns:
            connection.execute(text("ALTER TABLE packs ADD COLUMN user_id INTEGER"))
