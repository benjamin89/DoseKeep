from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.catalogue import CatalogueProduct
from app.database import Base
from app.models import Pack, Product, User
from app.schemas import ScannedPackCreate


def test_non_serialised_repeat_scan_creates_a_second_pack(monkeypatch):
    """A GTIN is not a unique identifier for a physical OTC pack."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(email="test@example.invalid", password_hash="hash", timezone="Europe/Paris")
    product = Product(name="Example OTC", barcode="03400930330531", category="otc")
    session.add_all([user, product])
    session.flush()
    session.add(Pack(
        user_id=user.id,
        product_id=product.id,
        gtin="03400930330531",
        batch_number="LOT-42",
        expiry_date=date(2028, 9, 30),
        quantity_initial=20,
        quantity_remaining=20,
    ))
    session.commit()

    monkeypatch.setattr(main, "parse_medicine_code", lambda _: {
        "gtin": "03400930330531",
        "serial_number": None,
        "batch_number": "LOT-42",
        "expiry_date": date(2028, 9, 30),
    })
    monkeypatch.setattr(main, "lookup_french_gtin", lambda _: CatalogueProduct(
        name="Example OTC", common_name=None, form=None, route=None, holder=None,
        presentation=None, quantity_hint=None,
    ))
    monkeypatch.setattr(main, "consolidate_medikeep_products", lambda *_: None)
    monkeypatch.setattr(main, "save_default_common_name", lambda *_: None)

    result = main.create_pack_from_scan(
        ScannedPackCreate(raw="repeat-scan", quantity_initial=20, category="otc"),
        user,
        session,
    )

    assert result["created"] is True
    assert session.query(Pack).filter_by(user_id=user.id, gtin="03400930330531").count() == 2
