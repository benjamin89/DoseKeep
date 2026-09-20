"""Database setup for DoseKeep's local operational records."""

import os

from sqlalchemy import create_engine
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

