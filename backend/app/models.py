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


class Pack(Base):
    __tablename__ = "packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    gtin: Mapped[str | None] = mapped_column(String(14), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    batch_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantity_initial: Mapped[int] = mapped_column(Integer)
    quantity_remaining: Mapped[int] = mapped_column(Integer)
    obtained_on: Mapped[date] = mapped_column(Date, default=date.today)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="packs")
    events: Mapped[list["SupplyEvent"]] = relationship(back_populates="pack")


class SupplyEvent(Base):
    __tablename__ = "supply_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("packs.id"))
    event_type: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    pack: Mapped[Pack] = relationship(back_populates="events")

