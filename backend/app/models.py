"""Operational medication-supply models.

Clinical medication history stays in MediKeep. DoseKeep owns products, packs,
dosette allocations and adherence events, and can later link a product to a
MediKeep medication id without requiring that integration.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    strength: Mapped[str | None] = mapped_column(String(100), nullable=True)
    form: Mapped[str | None] = mapped_column(String(100), nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="medicine")
    barcode: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    catalogue_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    medikeep_medication_id: Mapped[int | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    packs: Mapped[list["Pack"]] = relationship(back_populates="product")
    medikeep_links: Mapped[list["MediKeepLink"]] = relationship(back_populates="product")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Paris")
    administration_times: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    packs: Mapped[list["Pack"]] = relationship(back_populates="user")
    medikeep_connection: Mapped["MediKeepConnection | None"] = relationship(back_populates="user")
    medikeep_links: Mapped[list["MediKeepLink"]] = relationship(back_populates="user")
    medication_schedules: Mapped[list["MedicationSchedule"]] = relationship(back_populates="user")
    scheduled_doses: Mapped[list["ScheduledDose"]] = relationship(back_populates="user")


class Pack(Base):
    __tablename__ = "packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    gtin: Mapped[str | None] = mapped_column(String(14), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    batch_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantity_initial: Mapped[int] = mapped_column(Integer)
    quantity_remaining: Mapped[int] = mapped_column(Integer)
    quantity_in_dosette: Mapped[int] = mapped_column(Integer, default=0)
    obtained_on: Mapped[date] = mapped_column(Date, default=date.today)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="packs")
    user: Mapped[User | None] = relationship(back_populates="packs")
    events: Mapped[list["SupplyEvent"]] = relationship(back_populates="pack")


class MediKeepConnection(Base):
    __tablename__ = "medikeep_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    encrypted_config: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="medikeep_connection")


class MediKeepLink(Base):
    __tablename__ = "medikeep_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    medikeep_medication_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="medikeep_links")
    product: Mapped[Product] = relationship(back_populates="medikeep_links")


class MedicationSchedule(Base):
    """User-specific regular/PRN administration plan for a tracked product."""

    __tablename__ = "medication_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    regular_times: Mapped[str] = mapped_column(Text, default="[]")
    as_required: Mapped[bool] = mapped_column(default=False)
    prn_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="medication_schedules")
    product: Mapped[Product] = relationship()


class ScheduledDose(Base):
    """A concrete administration opportunity, generated from a regular plan."""

    __tablename__ = "scheduled_doses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    administration_time: Mapped[str] = mapped_column(String(20))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="due", index=True)
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="scheduled_doses")
    product: Mapped[Product] = relationship()


class SupplyEvent(Base):
    __tablename__ = "supply_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("packs.id"))
    event_type: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    pack: Mapped[Pack] = relationship(back_populates="events")
