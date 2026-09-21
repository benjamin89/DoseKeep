import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, decrypt_config, encrypt_config, hash_password, master_key, verify_password
from .catalogue import lookup_french_gtin
from .database import Base, engine, ensure_schema, get_session
from .gs1 import parse_medicine_code
from .medikeep import MediKeepUnavailable, active_medications, all_medications, normalize_name, suggested_medications
from .models import MediKeepConnection, MediKeepLink, MedicationSchedule, Pack, Product, ScheduledDose, SupplyEvent, User
from .schemas import (
    CatalogueProductRead,
    AuthStatus,
    DecodedCode,
    MediKeepImportRead,
    MediKeepConnectionCreate,
    MediKeepConnectionRead,
    MediKeepMedicationRead,
    MedicineOverviewRead,
    MedicationScheduleUpdate,
    ScheduledDoseAction,
    ScheduledDoseRead,
    PackCreate,
    PackRead,
    ProductCreate,
    ProductRead,
    ScannedPackCreate,
    ScannedPackRead,
    StockCorrection,
    SupplyEventCreate,
    SupplyEventRead,
    UserRegister,
)


Base.metadata.create_all(bind=engine)
ensure_schema()

app = FastAPI(title="DoseKeep", version="0.5.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=master_key(),
    https_only=True,
    same_site="lax",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dosekeep"}


@app.get("/api/auth/status", response_model=AuthStatus)
def auth_status(request: Request, session: Session = Depends(get_session)):
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    if not user:
        return {"authenticated": False, "user": None, "medikeep_connected": False}
    return {
        "authenticated": True,
        "user": user,
        "medikeep_connected": bool(session.query(MediKeepConnection).filter_by(user_id=user.id).first()),
    }


@app.post("/api/auth/register", response_model=AuthStatus, status_code=201)
def register_account(payload: UserRegister, request: Request, session: Session = Depends(get_session)):
    email = payload.email.strip().lower()
    if session.query(User).filter_by(email=email).first():
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    first_account = session.query(User).count() == 0
    user = User(email=email, password_hash=hash_password(payload.password))
    session.add(user)
    session.flush()
    # The first account on an existing single-user install owns legacy packs.
    if first_account:
        session.query(Pack).filter(Pack.user_id.is_(None)).update({Pack.user_id: user.id})
    session.commit()
    request.session["user_id"] = user.id
    return {"authenticated": True, "user": user, "medikeep_connected": False}


@app.post("/api/auth/login", response_model=AuthStatus)
def login_account(payload: UserRegister, request: Request, session: Session = Depends(get_session)):
    user = session.query(User).filter_by(email=payload.email.strip().lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    request.session["user_id"] = user.id
    return {
        "authenticated": True,
        "user": user,
        "medikeep_connected": bool(session.query(MediKeepConnection).filter_by(user_id=user.id).first()),
    }


@app.post("/api/auth/logout")
def logout_account(request: Request):
    request.session.clear()
    return {"ok": True}


@app.post("/api/v1/scan/parse", response_model=DecodedCode)
def parse_scan(raw: str, user: User = Depends(current_user)):
    try:
        return parse_medicine_code(raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/catalogue/fr/{gtin}", response_model=CatalogueProductRead)
def lookup_french_catalogue(gtin: str, user: User = Depends(current_user)):
    product = lookup_french_gtin(gtin)
    if not product:
        raise HTTPException(status_code=404, detail="No active French catalogue match")
    return {"gtin": gtin, **product.__dict__}


def medikeep_read(medication) -> dict:
    return medication.__dict__


def migrate_legacy_links(user: User, session: Session) -> None:
    """Carry forward links made before links became user-specific."""
    legacy_products = (
        session.query(Product)
        .join(Pack, Pack.product_id == Product.id)
        .filter(Pack.user_id == user.id, Product.medikeep_medication_id.is_not(None))
        .distinct()
        .all()
    )
    changed = False
    for product in legacy_products:
        exists = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product.id).first()
        if not exists:
            session.add(
                MediKeepLink(
                    user_id=user.id,
                    product_id=product.id,
                    medikeep_medication_id=product.medikeep_medication_id,
                )
            )
            changed = True
    if changed:
        session.commit()


def auto_link_clear_matches(user: User, session: Session, products: dict[int, Product], medications: dict) -> None:
    """Link only a uniquely strong product/medicine match.

    This repairs the common generic-name case (for example, a branded pack
    containing atorvastatin) without guessing between similarly named drugs.
    Unclear matches still need the user to choose at scan time.
    """
    existing_product_ids = {
        item.product_id for item in session.query(MediKeepLink).filter_by(user_id=user.id).all()
    }
    changed = False
    for product_id, product in products.items():
        if product_id in existing_product_ids or product.category != "medicine":
            continue
        product_tokens = normalize_name(f"{product.name} {product.strength or ''}")
        scored = []
        for medication in medications.values():
            medication_tokens = normalize_name(f"{medication.name} {medication.dosage or ''}")
            score = len(product_tokens & medication_tokens)
            if score:
                scored.append((score, medication))
        scored.sort(key=lambda entry: (-entry[0], entry[1].name))
        if not scored or scored[0][0] < 2:
            continue
        best_score, best = scored[0]
        if len(scored) > 1 and scored[1][0] == best_score:
            continue
        session.add(MediKeepLink(user_id=user.id, product_id=product_id, medikeep_medication_id=best.id))
        changed = True
    if changed:
        session.commit()


def user_medikeep_config(user: User, session: Session) -> dict:
    connection = session.query(MediKeepConnection).filter_by(user_id=user.id).first()
    if not connection:
        raise HTTPException(status_code=503, detail="MediKeep has not been connected for this account")
    try:
        return decrypt_config(connection.encrypted_config)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Saved MediKeep connection could not be read") from error


@app.get("/api/v1/medikeep/connection", response_model=MediKeepConnectionRead)
def medikeep_connection(user: User = Depends(current_user), session: Session = Depends(get_session)):
    connection = session.query(MediKeepConnection).filter_by(user_id=user.id).first()
    if not connection:
        return {"configured": False, "base_url": None}
    config = user_medikeep_config(user, session)
    return {"configured": True, "base_url": config.get("base_url")}


@app.post("/api/v1/medikeep/connection", response_model=MediKeepConnectionRead)
def save_medikeep_connection(
    payload: MediKeepConnectionCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        payload.validate_credentials()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    config = {
        "base_url": payload.base_url.rstrip("/"),
        "patient_id": payload.patient_id,
        "token": payload.token or "",
        "username": payload.username or "",
        "password": payload.password or "",
    }
    try:
        active_medications(config)  # Test before persisting credentials.
    except MediKeepUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    connection = session.query(MediKeepConnection).filter_by(user_id=user.id).first()
    if connection:
        connection.encrypted_config = encrypt_config(config)
    else:
        connection = MediKeepConnection(user_id=user.id, encrypted_config=encrypt_config(config))
        session.add(connection)
    session.commit()
    return {"configured": True, "base_url": config["base_url"]}


@app.get("/api/v1/medikeep/active-medications", response_model=list[MediKeepMedicationRead])
def list_medikeep_active_medications(user: User = Depends(current_user), session: Session = Depends(get_session)):
    try:
        return [medikeep_read(medication) for medication in active_medications(user_medikeep_config(user, session))]
    except MediKeepUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/v1/medikeep/suggestions", response_model=list[MediKeepMedicationRead])
def list_medikeep_suggestions(name: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    try:
        return [medikeep_read(medication) for medication in suggested_medications(user_medikeep_config(user, session), name)]
    except MediKeepUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/medikeep/import/{medication_id}", response_model=MediKeepImportRead)
def import_medikeep_medication(medication_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Create or explicitly link a DoseKeep product from an active MediKeep record."""
    try:
        medication = next((item for item in all_medications(user_medikeep_config(user, session)) if item.id == medication_id), None)
    except MediKeepUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not medication:
        raise HTTPException(status_code=404, detail="Active MediKeep medication not found")
    link = session.query(MediKeepLink).filter_by(user_id=user.id, medikeep_medication_id=medication.id).first()
    product = link.product if link else None
    created = False
    if not product:
        medication_tokens = normalize_name(f"{medication.name} {medication.dosage or ''}")
        candidates = session.query(Product).all()
        product = next(
            (item for item in candidates if medication_tokens & normalize_name(item.name)),
            None,
        )
        if not product:
            product = Product(
                name=medication.name,
                strength=medication.dosage,
                category="medicine",
                catalogue_source="MediKeep (read-only import)",
            )
            session.add(product)
            created = True
        session.flush()
        session.add(MediKeepLink(user_id=user.id, product_id=product.id, medikeep_medication_id=medication.id))
    session.commit()
    session.refresh(product)
    return {"product": product, "created": created}


@app.get("/api/v1/products", response_model=list[ProductRead])
def list_products(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return (
        session.query(Product)
        .outerjoin(Pack, Pack.product_id == Product.id)
        .outerjoin(MediKeepLink, MediKeepLink.product_id == Product.id)
        .filter(or_(Pack.user_id == user.id, MediKeepLink.user_id == user.id))
        .distinct()
        .order_by(Product.name)
        .all()
    )


@app.get("/api/v1/dashboard/medicines", response_model=list[MedicineOverviewRead])
def list_medicine_dashboard(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Default user view: medicines first, with packs as supporting detail."""
    packs = session.query(Pack).filter_by(user_id=user.id, status="active").all()
    product_ids = {pack.product_id for pack in packs}
    products = {item.id: item for item in session.query(Product).filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    all_by_id = {}
    try:
        all_by_id = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        # DoseKeep's own stock dashboard remains useful when MediKeep is offline.
        pass

    migrate_legacy_links(user, session)
    if all_by_id:
        auto_link_clear_matches(user, session, products, all_by_id)
    links = session.query(MediKeepLink).filter_by(user_id=user.id).all()
    product_ids |= {link.product_id for link in links}
    if product_ids:
        products = {item.id: item for item in session.query(Product).filter(Product.id.in_(product_ids)).all()}

    links_by_product = {link.product_id: link for link in links}
    schedules = {item.product_id: item for item in session.query(MedicationSchedule).filter_by(user_id=user.id).all()}
    schedules_by_medikeep = {}
    for product_id, schedule in schedules.items():
        link = links_by_product.get(product_id)
        if link:
            schedules_by_medikeep.setdefault(link.medikeep_medication_id, schedule)
    cards: dict[str, dict] = {}
    for product_id, product in products.items():
        link = links_by_product.get(product_id)
        external = all_by_id.get(link.medikeep_medication_id) if link else None
        schedule = schedules.get(product_id) or (schedules_by_medikeep.get(link.medikeep_medication_id) if link else None)
        matching_packs = [pack for pack in packs if pack.product_id == product_id]
        card_key = f"medikeep:{link.medikeep_medication_id}" if link else f"product:{product_id}"
        # A person may have an old and a new pack/product for the same
        # MediKeep medicine. Show one medicine with combined stock, not two.
        if card_key in cards:
            cards[card_key]["quantity_remaining"] += sum(pack.quantity_remaining for pack in matching_packs)
            cards[card_key]["quantity_in_dosette"] += sum(pack.quantity_in_dosette for pack in matching_packs)
            cards[card_key]["active_pack_count"] += len(matching_packs)
            continue
        cards[card_key] = {
            "key": card_key,
            "product_id": product_id,
            "medikeep_medication_id": link.medikeep_medication_id if link else None,
            "name": external.name if external else product.name,
            "dosage": external.dosage if external else product.strength,
            "route": external.route if external else None,
            "frequency": external.frequency if external else None,
            "quantity_remaining": sum(pack.quantity_remaining for pack in matching_packs),
            "quantity_in_dosette": sum(pack.quantity_in_dosette for pack in matching_packs),
            "active_pack_count": len(matching_packs),
            "linked_to_medikeep": bool(link),
            "medikeep_status": external.status if external else None,
            "regular_times": json.loads(schedule.regular_times) if schedule else [],
            "as_required": schedule.as_required if schedule else False,
            "prn_notes": schedule.prn_notes if schedule else None,
        }

    # Show active MediKeep medicines even before the user has scanned a pack.
    # Non-active entries remain shown once linked to stock above.
    linked_medication_ids = {link.medikeep_medication_id for link in links}
    for medication_id, medication in all_by_id.items():
        if medication.status == "active" and medication_id not in linked_medication_ids:
            cards[f"medikeep:{medication_id}"] = {
                "key": f"medikeep:{medication_id}",
                "product_id": None,
                "medikeep_medication_id": medication_id,
                "name": medication.name,
                "dosage": medication.dosage,
                "route": medication.route,
                "frequency": medication.frequency,
                "quantity_remaining": 0,
                "quantity_in_dosette": 0,
                "active_pack_count": 0,
                "linked_to_medikeep": True,
                "medikeep_status": medication.status,
                "regular_times": [],
                "as_required": False,
                "prn_notes": None,
            }
    return sorted(cards.values(), key=lambda item: item["name"].casefold())


@app.put("/api/v1/products/{product_id}/schedule", response_model=MedicineOverviewRead)
def update_medication_schedule(
    product_id: int,
    payload: MedicationScheduleUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        payload.validate_schedule()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    owns_product = session.query(Pack).filter_by(user_id=user.id, product_id=product_id).first()
    linked_product = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first()
    if not owns_product and not linked_product:
        raise HTTPException(status_code=404, detail="Medicine not found")
    link = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first()
    related_product_ids = [product_id]
    if link:
        related_product_ids = [
            item.product_id
            for item in session.query(MediKeepLink)
            .filter_by(user_id=user.id, medikeep_medication_id=link.medikeep_medication_id)
            .all()
        ]
    # One plan belongs to one medicine, not every historic pack/product that
    # happens to be linked to it. Retire duplicate per-product plans first.
    if len(related_product_ids) > 1:
        session.query(MedicationSchedule).filter(
            MedicationSchedule.user_id == user.id,
            MedicationSchedule.product_id.in_(related_product_ids),
            MedicationSchedule.product_id != product_id,
        ).delete(synchronize_session=False)
    schedule = session.query(MedicationSchedule).filter_by(user_id=user.id, product_id=product_id).first()
    if not schedule:
        schedule = MedicationSchedule(user_id=user.id, product_id=product_id)
        session.add(schedule)
    schedule.regular_times = json.dumps(sorted(set(payload.regular_times)))
    schedule.as_required = payload.as_required
    schedule.prn_notes = payload.prn_notes.strip() if payload.prn_notes else None
    session.commit()
    return next(item for item in list_medicine_dashboard(user, session) if item["product_id"] == product_id)


SLOT_CLOCKS = {
    "morning": time(8, 0),
    "midday": time(12, 0),
    "evening": time(19, 0),
    "bedtime": time(22, 0),
}


def user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "Europe/Paris")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Paris")


def generate_today_doses(user: User, session: Session) -> None:
    """Materialise today’s regular plan as actionable doses, idempotently."""
    zone = user_zone(user)
    local_today = datetime.now(timezone.utc).astimezone(zone).date()
    all_by_id = {}
    try:
        all_by_id = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        pass
    links = {item.product_id: item for item in session.query(MediKeepLink).filter_by(user_id=user.id).all()}
    schedules = session.query(MedicationSchedule).filter_by(user_id=user.id).all()
    generated = False
    seen_medikeep_ids = set()
    for schedule in schedules:
        link = links.get(schedule.product_id)
        if link:
            # A shared medicine may have historic product links: only its plan
            # record should generate a dose, and stopped prescriptions do not.
            if link.medikeep_medication_id in seen_medikeep_ids:
                continue
            seen_medikeep_ids.add(link.medikeep_medication_id)
            external = all_by_id.get(link.medikeep_medication_id)
            if external and external.status != "active":
                continue
        for slot in json.loads(schedule.regular_times):
            clock = SLOT_CLOCKS.get(slot)
            if not clock:
                continue
            local_due = datetime.combine(local_today, clock, tzinfo=zone)
            due = local_due.astimezone(timezone.utc).replace(tzinfo=None)
            exists = session.query(ScheduledDose).filter_by(
                user_id=user.id,
                product_id=schedule.product_id,
                administration_time=slot,
                scheduled_for=due,
            ).first()
            if not exists:
                session.add(
                    ScheduledDose(
                        user_id=user.id,
                        product_id=schedule.product_id,
                        administration_time=slot,
                        scheduled_for=due,
                        due_at=due,
                    )
                )
                generated = True
    if generated:
        session.commit()


def dose_read(dose: ScheduledDose, session: Session) -> dict:
    product = session.get(Product, dose.product_id)
    stock = sum(
        pack.quantity_remaining + pack.quantity_in_dosette
        for pack in session.query(Pack).filter_by(user_id=dose.user_id, product_id=dose.product_id, status="active").all()
    )
    return {
        "id": dose.id,
        "product_id": dose.product_id,
        "medicine_name": product.name if product else "Unknown medicine",
        "dosage": product.strength if product else None,
        "route": None,
        "administration_time": dose.administration_time,
        "scheduled_for": dose.scheduled_for,
        "due_at": dose.due_at,
        "status": dose.status,
        "actioned_at": dose.actioned_at,
        "notes": dose.notes,
        "stock_available": stock,
    }


@app.get("/api/v1/doses/today", response_model=list[ScheduledDoseRead])
def list_today_doses(user: User = Depends(current_user), session: Session = Depends(get_session)):
    generate_today_doses(user, session)
    zone = user_zone(user)
    local_today = datetime.now(timezone.utc).astimezone(zone).date()
    start = datetime.combine(local_today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    doses = (
        session.query(ScheduledDose)
        .filter(ScheduledDose.user_id == user.id, ScheduledDose.scheduled_for >= start, ScheduledDose.scheduled_for < end)
        .order_by(ScheduledDose.due_at)
        .all()
    )
    return [dose_read(dose, session) for dose in doses]


@app.post("/api/v1/doses/{dose_id}/action", response_model=ScheduledDoseRead)
def action_scheduled_dose(
    dose_id: int,
    payload: ScheduledDoseAction,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    dose = session.query(ScheduledDose).filter_by(id=dose_id, user_id=user.id).first()
    if not dose:
        raise HTTPException(status_code=404, detail="Scheduled dose not found")
    if dose.status in {"taken", "skipped"}:
        raise HTTPException(status_code=409, detail="This dose has already been actioned")
    now = datetime.utcnow()
    if payload.action == "snooze":
        dose.status = "snoozed"
        dose.due_at = now + timedelta(minutes=payload.snooze_minutes)
        dose.notes = payload.notes or f"Snoozed for {payload.snooze_minutes} minutes"
    elif payload.action == "skipped":
        dose.status = "skipped"
        dose.actioned_at = now
        dose.notes = payload.notes
    else:
        pack = (
            session.query(Pack)
            .filter(Pack.user_id == user.id, Pack.product_id == dose.product_id, Pack.status == "active", Pack.quantity_remaining > 0)
            .order_by(Pack.expiry_date.is_(None), Pack.expiry_date, Pack.id)
            .first()
        )
        if not pack:
            raise HTTPException(status_code=409, detail="No recorded pack has stock for this medicine")
        pack.quantity_remaining -= 1
        session.add(SupplyEvent(pack_id=pack.id, event_type="taken_from_pack", quantity=1, notes=f"Scheduled {dose.administration_time} dose"))
        dose.status = "taken"
        dose.actioned_at = now
        dose.notes = payload.notes
    session.commit()
    session.refresh(dose)
    return dose_read(dose, session)


@app.post("/api/v1/products", response_model=ProductRead, status_code=201)
def create_product(product: ProductCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    record = Product(**product.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.get("/api/v1/packs", response_model=list[PackRead])
def list_packs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return session.query(Pack).filter(Pack.user_id == user.id).order_by(Pack.expiry_date.is_(None), Pack.expiry_date).all()


@app.get("/api/v1/packs/{pack_id}/events", response_model=list[SupplyEventRead])
def list_pack_events(pack_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not session.query(Pack).filter_by(id=pack_id, user_id=user.id).first():
        raise HTTPException(status_code=404, detail="Pack not found")
    return (
        session.query(SupplyEvent)
        .filter(SupplyEvent.pack_id == pack_id)
        .order_by(SupplyEvent.occurred_at.desc())
        .limit(10)
        .all()
    )


@app.post("/api/v1/packs", response_model=PackRead, status_code=201)
def create_pack(pack: PackCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not session.get(Product, pack.product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    record = Pack(**pack.model_dump(exclude_none=True), user_id=user.id, obtained_on=pack.obtained_on or date.today())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.post("/api/v1/packs/from-scan", response_model=ScannedPackRead, status_code=201)
def create_pack_from_scan(scan: ScannedPackCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    try:
        decoded = parse_medicine_code(scan.raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    gtin = decoded.get("gtin")
    if not gtin:
        raise HTTPException(status_code=422, detail="The scan does not contain a GTIN")
    catalogue_product = lookup_french_gtin(str(gtin))
    if not catalogue_product:
        raise HTTPException(status_code=404, detail="No French catalogue match; create the product manually")

    if scan.medikeep_medication_id is not None:
        try:
            available_ids = {item.id for item in all_medications(user_medikeep_config(user, session))}
        except MediKeepUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if scan.medikeep_medication_id not in available_ids:
            raise HTTPException(status_code=422, detail="Selected MediKeep medication was not found")

    existing_pack = session.query(Pack).filter(Pack.user_id == user.id, Pack.gtin == gtin, Pack.serial_number == decoded.get("serial_number")).first()
    if existing_pack:
        return {"product": existing_pack.product, "pack": existing_pack, "created": False}

    product = session.query(Product).filter(Product.barcode == gtin).first()
    if not product:
        product = Product(
            name=catalogue_product.name,
            form=catalogue_product.form,
            category=scan.category,
            barcode=gtin,
            catalogue_source=catalogue_product.source,
        )
        session.add(product)
        session.flush()
    if scan.medikeep_medication_id is not None:
        existing_link = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product.id).first()
        if not existing_link:
            session.add(MediKeepLink(user_id=user.id, product_id=product.id, medikeep_medication_id=scan.medikeep_medication_id))
    pack = Pack(
        user_id=user.id,
        product_id=product.id,
        gtin=gtin,
        serial_number=decoded.get("serial_number"),
        batch_number=decoded.get("batch_number"),
        expiry_date=decoded.get("expiry_date"),
        quantity_initial=scan.quantity_initial,
        quantity_remaining=scan.quantity_initial,
        obtained_on=scan.obtained_on or date.today(),
    )
    session.add(pack)
    session.commit()
    session.refresh(product)
    session.refresh(pack)
    return {"product": product, "pack": pack, "created": True}


@app.post("/api/v1/packs/{pack_id}/stock", response_model=PackRead)
def correct_pack_stock(pack_id: int, correction: StockCorrection, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Record a physical count without inventing historical dose events."""
    pack = session.query(Pack).filter_by(id=pack_id, user_id=user.id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Pack not found")
    previous = pack.quantity_remaining
    if previous != correction.quantity_remaining:
        pack.quantity_remaining = correction.quantity_remaining
        session.add(
            SupplyEvent(
                pack_id=pack_id,
                event_type="correction",
                quantity=abs(correction.quantity_remaining - previous),
                notes=correction.notes or f"Physical count adjusted from {previous} to {correction.quantity_remaining}",
            )
        )
    session.commit()
    session.refresh(pack)
    return pack


@app.post("/api/v1/packs/{pack_id}/events", response_model=SupplyEventRead, status_code=201)
def add_supply_event(pack_id: int, event: SupplyEventCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    pack = session.query(Pack).filter_by(id=pack_id, user_id=user.id).first()
    if not pack:
        raise HTTPException(status_code=404, detail="Pack not found")
    if event.event_type == "dosette_fill":
        if pack.quantity_remaining < event.quantity:
            raise HTTPException(status_code=409, detail="Insufficient pack stock")
        pack.quantity_remaining -= event.quantity
        pack.quantity_in_dosette += event.quantity
    elif event.event_type == "taken":
        if pack.quantity_in_dosette < event.quantity:
            raise HTTPException(status_code=409, detail="Insufficient dosette stock")
        pack.quantity_in_dosette -= event.quantity
    elif event.event_type == "taken_from_pack":
        if pack.quantity_remaining < event.quantity:
            raise HTTPException(status_code=409, detail="Insufficient pack stock")
        pack.quantity_remaining -= event.quantity
    elif event.event_type == "disposed":
        if pack.quantity_remaining < event.quantity:
            raise HTTPException(status_code=409, detail="Insufficient pack stock")
        pack.quantity_remaining -= event.quantity
    record = SupplyEvent(pack_id=pack_id, **event.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
