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
        if "household_id" not in columns:
            connection.execute(text("ALTER TABLE packs ADD COLUMN household_id INTEGER"))
        user_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(users)"))}
        if "timezone" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Paris'"))
        if "administration_times" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN administration_times TEXT NOT NULL DEFAULT '{}'"))
        if "notification_settings" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN notification_settings TEXT NOT NULL DEFAULT '{}'"))
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
        if not connection.execute(text("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1")).first():
            connection.execute(text("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"))
        dose_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(scheduled_doses)"))}
        if dose_columns and "notified_at" not in dose_columns:
            connection.execute(text("ALTER TABLE scheduled_doses ADD COLUMN notified_at DATETIME"))
        event_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(supply_events)"))}
        if event_columns and "actor_name" not in event_columns:
            connection.execute(text("ALTER TABLE supply_events ADD COLUMN actor_name TEXT"))
        guest_link_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(cabinet_guest_links)"))}
        if guest_link_columns and "encrypted_token" not in guest_link_columns:
            connection.execute(text("ALTER TABLE cabinet_guest_links ADD COLUMN encrypted_token TEXT"))
        invite_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(household_invites)"))}
        if invite_columns and "add_to_household" not in invite_columns:
            connection.execute(text("ALTER TABLE household_invites ADD COLUMN add_to_household BOOLEAN NOT NULL DEFAULT 1"))
