from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import reconcile_duplicate_today_doses
from app.models import Pack, Product, ScheduledDose, User


def test_prepared_dose_supersedes_same_named_active_product_duplicate():
    """A stale active product must not create a second pill-box tablet."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(email="test@example.invalid", password_hash="hash", timezone="Europe/Paris")
    first = Product(name="Perindopril tosilate/Indapamide")
    duplicate = Product(name="Perindopril tosilate/Indapamide")
    session.add_all([user, first, duplicate])
    session.flush()
    session.add_all([
        Pack(user_id=user.id, product_id=first.id, quantity_initial=1, quantity_remaining=1),
        Pack(user_id=user.id, product_id=duplicate.id, quantity_initial=1, quantity_remaining=1),
    ])
    scheduled_for = datetime(2026, 10, 11, 6, 0)
    prepared = ScheduledDose(
        user_id=user.id,
        product_id=first.id,
        administration_time="morning",
        scheduled_for=scheduled_for,
        due_at=scheduled_for,
        prepared_at=datetime.utcnow(),
    )
    unprepared = ScheduledDose(
        user_id=user.id,
        product_id=duplicate.id,
        administration_time="morning",
        scheduled_for=scheduled_for,
        due_at=scheduled_for,
    )
    session.add_all([prepared, unprepared])
    session.commit()

    reconcile_duplicate_today_doses(
        user,
        session,
        scheduled_for,
        scheduled_for + timedelta(days=1),
        ZoneInfo("Europe/Paris"),
    )

    session.refresh(prepared)
    session.refresh(unprepared)
    assert prepared.status == "due"
    assert unprepared.status == "superseded"
    assert unprepared.notes == "Superseded duplicate scheduled dose"
