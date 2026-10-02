import asyncio
import base64
import html
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from math import ceil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .auth import current_user, decrypt_config, encrypt_config, hash_password, master_key, verify_password
from .catalogue import lookup_french_gtin
from .database import Base, SessionLocal, engine, ensure_schema, get_session
from .gs1 import parse_medicine_code
from .medikeep import MediKeepUnavailable, active_medications, all_medications, create_medication, normalize_name, suggested_medications
from .models import CabinetGuestLink, DeviceToken, Household, HouseholdInvite, HouseholdMembership, MediKeepConnection, MediKeepLink, MedicationSchedule, NotificationActionLink, Pack, PillBoxAllocation, Product, ProductCommonName, ScheduledDose, SupplyEvent, User
from .notifications import send_ntfy
from .schemas import (
    AdministrationTimeSettings,
    CatalogueProductRead,
    CommonNameUpdate,
    DeviceTokenCreate,
    DeviceTokenCreated,
    DeviceTokenRead,
    AuthStatus,
    CabinetGuestLinkCreate,
    DecodedCode,
    MediKeepImportRead,
    HouseholdCreate,
    HouseholdInviteCreate,
    HouseholdMemberCreate,
    HouseholdRead,
    MediKeepConnectionCreate,
    MediKeepConnectionRead,
    MediKeepMedicationRead,
    MedicineOverviewRead,
    MedicationScheduleUpdate,
    NotificationSettings,
    PRNDoseCreate,
    ScheduledDoseAction,
    ScheduledDoseRead,
    PackCreate,
    PackHouseholdUpdate,
    PackRead,
    PillBoxPrepareRequest,
    PillBoxPrepareResult,
    PillBoxRoutineSettings,
    PillBoxTakeRequest,
    PillBoxTakeResult,
    PreparedDoseResolution,
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Keep reminders working while the web UI is closed."""
    task = asyncio.create_task(notification_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DoseKeep", version="0.8.3", lifespan=lifespan)
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
    user = User(email=email, password_hash=hash_password(payload.password), is_admin=first_account)
    session.add(user)
    session.flush()
    # The first account on an existing single-user install owns legacy packs.
    if first_account:
        session.query(Pack).filter(Pack.user_id.is_(None)).update({Pack.user_id: user.id})
    apply_pending_household_invite(request, user, session)
    session.commit()
    request.session["user_id"] = user.id
    return {"authenticated": True, "user": user, "medikeep_connected": False}


@app.post("/api/auth/login", response_model=AuthStatus)
def login_account(payload: UserRegister, request: Request, session: Session = Depends(get_session)):
    user = session.query(User).filter_by(email=payload.email.strip().lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    apply_pending_household_invite(request, user, session)
    session.commit()
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


DEFAULT_SLOT_TIMES = {"morning": "08:00", "midday": "12:00", "evening": "19:00", "bedtime": "22:00"}
DEFAULT_NOTIFICATION_SETTINGS = {"enabled": False, "server_url": "https://ntfy.sh", "topic": None}
HOUSEHOLD_ROLES = {"viewer": 0, "contributor": 1, "admin": 2}


def household_role(user: User, household_id: int, session: Session) -> str | None:
    membership = session.query(HouseholdMembership).filter_by(household_id=household_id, user_id=user.id).first()
    return membership.role if membership else None


def require_household_role(user: User, household_id: int, minimum: str, session: Session) -> None:
    role = household_role(user, household_id, session)
    if not role or HOUSEHOLD_ROLES.get(role, -1) < HOUSEHOLD_ROLES[minimum]:
        raise HTTPException(status_code=403, detail=f"{minimum.title()} access is required for this household")


def accessible_household_ids(user: User, session: Session) -> list[int]:
    return [item.household_id for item in session.query(HouseholdMembership).filter_by(user_id=user.id).all()]


def accessible_pack_filter(user: User, session: Session):
    household_ids = accessible_household_ids(user, session)
    return or_(Pack.user_id == user.id, Pack.household_id.in_(household_ids) if household_ids else False)


def can_change_pack(user: User, pack: Pack, session: Session) -> bool:
    if pack.user_id == user.id:
        return True
    return bool(pack.household_id and (role := household_role(user, pack.household_id, session)) and HOUSEHOLD_ROLES.get(role, -1) >= HOUSEHOLD_ROLES["contributor"])


def require_system_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="System administrator access is required")


def cabinet_guest_link(token: str, session: Session) -> CabinetGuestLink:
    record = session.query(CabinetGuestLink).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not record or record.revoked_at:
        raise HTTPException(status_code=404, detail="This cabinet link is no longer available")
    return record


def cabinet_guest_url(record: CabinetGuestLink) -> str | None:
    if not record.encrypted_token:
        return None
    try:
        token = decrypt_config(record.encrypted_token)["token"]
    except Exception:
        return None
    return f"{os.getenv('DOSEKEEP_PUBLIC_URL', 'https://dosekeep.godsil.co.uk').rstrip('/')}/cabinet/{token}"


def cabinet_qr_data_url(url: str) -> str:
    image = qrcode.make(url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


def household_display_names(household: Household, session: Session) -> dict[int, str]:
    """Use the household creator's editable common names for guest-facing stock."""
    return {
        item.product_id: item.common_name
        for item in session.query(ProductCommonName).filter_by(user_id=household.created_by_user_id).all()
    }


def household_invite_url(record: HouseholdInvite) -> str | None:
    try:
        token = decrypt_config(record.encrypted_token)["token"]
    except Exception:
        return None
    return f"{os.getenv('DOSEKEEP_PUBLIC_URL', 'https://dosekeep.godsil.co.uk').rstrip('/')}/invite/{token}"


def apply_pending_household_invite(request: Request, user: User, session: Session) -> None:
    invite_id = request.session.pop("household_invite_id", None)
    if not invite_id:
        return
    invite = session.get(HouseholdInvite, invite_id)
    if not invite or invite.revoked_at or invite.accepted_at:
        return
    if invite.household_id and invite.add_to_household:
        membership = session.query(HouseholdMembership).filter_by(household_id=invite.household_id, user_id=user.id).first()
        if membership:
            membership.role = invite.role
        else:
            session.add(HouseholdMembership(household_id=invite.household_id, user_id=user.id, role=invite.role))
    invite.accepted_by_user_id = user.id
    invite.accepted_at = datetime.utcnow()


@app.get("/api/v1/admin/overview")
def system_admin_overview(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Minimal operational overview; it deliberately excludes health details."""
    require_system_admin(user)
    users = session.query(User).order_by(User.created_at).all()
    return {
        "counts": {
            "users": len(users),
            "households": session.query(Household).count(),
            "active_packs": session.query(Pack).filter_by(status="active").count(),
            "active_guest_links": session.query(CabinetGuestLink).filter(CabinetGuestLink.revoked_at.is_(None)).count(),
        },
        "users": [
            {
                "email": item.email,
                "is_admin": item.is_admin,
                "households": session.query(HouseholdMembership).filter_by(user_id=item.id).count(),
                "created_at": item.created_at,
            }
            for item in users
        ],
    }


def administration_times(user: User) -> dict[str, str]:
    try:
        configured = json.loads(user.administration_times or "{}")
    except (TypeError, ValueError):
        configured = {}
    return {slot: configured.get(slot, default) for slot, default in DEFAULT_SLOT_TIMES.items()}


def notification_settings(user: User) -> dict:
    try:
        configured = json.loads(user.notification_settings or "{}")
    except (TypeError, ValueError):
        configured = {}
    return {**DEFAULT_NOTIFICATION_SETTINGS, **configured}


DEFAULT_PILL_BOX_SETTINGS = {
    "enabled": False,
    "top_up_weekday": 6,
    "stock_check_weekday": 1,
    "days": 7,
    "weekdays": list(range(7)),
    "slots": ["morning", "midday", "evening", "bedtime"],
}


def pill_box_settings(user: User) -> dict:
    try:
        configured = json.loads(user.pill_box_settings or "{}")
    except (TypeError, ValueError):
        configured = {}
    return {**DEFAULT_PILL_BOX_SETTINGS, **configured}


@app.get("/api/v1/settings/pill-box", response_model=PillBoxRoutineSettings)
def get_pill_box_settings(user: User = Depends(current_user)):
    return pill_box_settings(user)


@app.put("/api/v1/settings/pill-box", response_model=PillBoxRoutineSettings)
def update_pill_box_settings(
    payload: PillBoxRoutineSettings,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        payload.validate_selection()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    notifications = notification_settings(user)
    if payload.enabled and (not notifications["enabled"] or not notifications["topic"]):
        raise HTTPException(status_code=422, detail="Enable ntfy medication reminders and set a topic before enabling a pill-box routine")
    # Changing the routine deliberately clears prior reminder dates so the
    # next applicable top-up/check is not suppressed by stale state.
    user.pill_box_settings = json.dumps(payload.model_dump())
    session.commit()
    return pill_box_settings(user)


@app.get("/api/v1/settings/administration-times", response_model=AdministrationTimeSettings)
def get_administration_times(user: User = Depends(current_user)):
    return administration_times(user)


@app.put("/api/v1/settings/administration-times", response_model=AdministrationTimeSettings)
def update_administration_times(
    payload: AdministrationTimeSettings,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    user.administration_times = json.dumps(payload.model_dump())
    zone = user_zone(user)
    today = datetime.now(timezone.utc).astimezone(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    # Pending doses are regenerated at the new times; already taken/skipped
    # records are retained as history.
    session.query(ScheduledDose).filter(
        ScheduledDose.user_id == user.id,
        ScheduledDose.scheduled_for >= start,
        ScheduledDose.scheduled_for < end,
        ScheduledDose.administration_time.in_(list(DEFAULT_SLOT_TIMES)),
        ScheduledDose.status.in_(("due", "snoozed")),
    ).delete(synchronize_session=False)
    session.commit()
    return administration_times(user)


@app.get("/api/v1/settings/notifications", response_model=NotificationSettings)
def get_notification_settings(user: User = Depends(current_user)):
    return notification_settings(user)


@app.put("/api/v1/settings/notifications", response_model=NotificationSettings)
def update_notification_settings(
    payload: NotificationSettings,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        payload.validate_destination()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    user.notification_settings = json.dumps(payload.model_dump())
    session.commit()
    return notification_settings(user)


@app.post("/api/v1/settings/notifications/test")
def test_notification_settings(user: User = Depends(current_user)):
    settings = notification_settings(user)
    if not settings["enabled"] or not settings["topic"]:
        raise HTTPException(status_code=422, detail="Enable reminders and enter an ntfy topic first")
    try:
        send_ntfy(settings["server_url"], settings["topic"], "DoseKeep test", "ntfy notifications are connected.")
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not send ntfy test: {error}") from error
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


def user_common_names(user: User, session: Session) -> dict[int, str]:
    return {
        item.product_id: item.common_name
        for item in session.query(ProductCommonName).filter_by(user_id=user.id).all()
    }


def save_default_common_name(user: User, product_id: int, common_name: str | None, session: Session) -> None:
    """Keep a scan/import suggestion without overwriting the user's edit."""
    if not common_name or session.query(ProductCommonName).filter_by(user_id=user.id, product_id=product_id).first():
        return
    session.add(ProductCommonName(user_id=user.id, product_id=product_id, common_name=common_name.strip()))


def consolidate_medikeep_products(user: User, session: Session) -> None:
    """Keep one stable DoseKeep medicine behind each linked MediKeep medicine.

    Earlier versions created a new Product for every barcode, even when the
    pack was linked to the same MediKeep medicine. Move those records to the
    original product so its chosen common name remains stable.
    """
    links = session.query(MediKeepLink).filter_by(user_id=user.id).order_by(MediKeepLink.product_id).all()
    by_medication: dict[int, list[MediKeepLink]] = {}
    for link in links:
        by_medication.setdefault(link.medikeep_medication_id, []).append(link)
    changed = False
    for related_links in by_medication.values():
        if len(related_links) < 2:
            continue
        anchor_id = related_links[0].product_id
        anchor_schedule = session.query(MedicationSchedule).filter_by(user_id=user.id, product_id=anchor_id).first()
        for duplicate_link in related_links[1:]:
            duplicate_id = duplicate_link.product_id
            session.query(Pack).filter_by(user_id=user.id, product_id=duplicate_id).update({Pack.product_id: anchor_id})
            duplicate_schedule = session.query(MedicationSchedule).filter_by(user_id=user.id, product_id=duplicate_id).first()
            if duplicate_schedule and not anchor_schedule:
                duplicate_schedule.product_id = anchor_id
                anchor_schedule = duplicate_schedule
            session.query(ScheduledDose).filter_by(user_id=user.id, product_id=duplicate_id).update({ScheduledDose.product_id: anchor_id})
            session.query(DeviceToken).filter_by(user_id=user.id, product_id=duplicate_id).update({DeviceToken.product_id: anchor_id})
            session.delete(duplicate_link)
            changed = True
    if changed:
        session.commit()


def migrate_legacy_links(user: User, session: Session) -> None:
    """Carry forward links made before links became user-specific."""
    # A pack may later have been exhausted/deleted while its MAR history is
    # still relevant. Include both pack and scheduled-dose ownership so those
    # historic records keep their original MediKeep identity in reports.
    product_ids = {
        product_id
        for product_id, in session.query(Pack.product_id).filter(Pack.user_id == user.id).distinct().all()
    }
    product_ids.update(
        product_id
        for product_id, in session.query(ScheduledDose.product_id).filter(ScheduledDose.user_id == user.id).distinct().all()
    )
    if not product_ids:
        return
    legacy_products = (
        session.query(Product)
        .filter(Product.id.in_(product_ids), Product.medikeep_medication_id.is_not(None))
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
        save_default_common_name(user, product.id, medication.name, session)
        session.add(MediKeepLink(user_id=user.id, product_id=product.id, medikeep_medication_id=medication.id))
    session.commit()
    session.refresh(product)
    return {"product": product, "created": created}


@app.post("/api/v1/products/{product_id}/medikeep", response_model=MediKeepMedicationRead, status_code=201)
def create_and_link_medikeep_medication(product_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Explicitly add a confirmed DoseKeep product to MediKeep, then link it."""
    product = session.get(Product, product_id)
    owned = product and (
        session.query(Pack).filter_by(user_id=user.id, product_id=product_id).first()
        or session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first()
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Product not found")
    if session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first():
        raise HTTPException(status_code=409, detail="This DoseKeep product is already linked to MediKeep")
    try:
        medication = create_medication(
            user_medikeep_config(user, session),
            name=user_common_names(user, session).get(product.id) or product.name,
            dosage=product.strength,
            category=product.category,
        )
    except MediKeepUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    session.add(MediKeepLink(user_id=user.id, product_id=product.id, medikeep_medication_id=medication.id))
    session.commit()
    return medikeep_read(medication)


@app.get("/api/v1/products", response_model=list[ProductRead])
def list_products(user: User = Depends(current_user), session: Session = Depends(get_session)):
    household_ids = accessible_household_ids(user, session)
    return (
        session.query(Product)
        .outerjoin(Pack, Pack.product_id == Product.id)
        .outerjoin(MediKeepLink, MediKeepLink.product_id == Product.id)
        .filter(or_(Pack.user_id == user.id, Pack.household_id.in_(household_ids) if household_ids else False, MediKeepLink.user_id == user.id))
        .distinct()
        .order_by(Product.name)
        .all()
    )


@app.get("/api/v1/dashboard/medicines", response_model=list[MedicineOverviewRead])
def list_medicine_dashboard(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Default user view: medicines first, with packs as supporting detail."""
    consolidate_medikeep_products(user, session)
    packs = session.query(Pack).filter(accessible_pack_filter(user, session), Pack.status == "active").all()
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
    common_names = user_common_names(user, session)
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
            "name": common_names.get(product_id) or (external.name if external else product.name),
            "common_name": common_names.get(product_id),
            "dosage": external.dosage if external else product.strength,
            "leaflet_url": product.leaflet_url,
            "route": external.route if external else None,
            "frequency": external.frequency if external else None,
            "quantity_remaining": sum(pack.quantity_remaining for pack in matching_packs),
            "quantity_in_dosette": sum(pack.quantity_in_dosette for pack in matching_packs),
            "active_pack_count": len(matching_packs),
            "linked_to_medikeep": bool(link),
            "medikeep_status": external.status if external else None,
            "regular_times": json.loads(schedule.regular_times) if schedule else [],
            "dose_quantities": schedule_dose_quantities(schedule),
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
                "common_name": None,
                "dosage": medication.dosage,
                "leaflet_url": None,
                "route": medication.route,
                "frequency": medication.frequency,
                "quantity_remaining": 0,
                "quantity_in_dosette": 0,
                "active_pack_count": 0,
                "linked_to_medikeep": True,
                "medikeep_status": medication.status,
                "regular_times": [],
                "dose_quantities": {},
                "as_required": False,
                "prn_notes": None,
            }
    # A run-out date is meaningful only for a regular plan. PRN usage varies,
    # so show stock but avoid inventing a misleading date for it.
    today = datetime.now(timezone.utc).astimezone(user_zone(user)).date()
    for card in cards.values():
        daily_dose_count = sum(card["dose_quantities"].get(slot, 1) for slot in card["regular_times"])
        available = card["quantity_remaining"] + card["quantity_in_dosette"]
        card["daily_dose_count"] = daily_dose_count
        if available <= 0:
            card["stock_status"] = "out_of_stock"
            card["estimated_run_out_date"] = today
        elif daily_dose_count:
            card["stock_status"] = "estimated"
            card["estimated_run_out_date"] = today + timedelta(days=ceil(available / daily_dose_count))
        else:
            card["stock_status"] = "unknown"
            card["estimated_run_out_date"] = None
    return sorted(cards.values(), key=lambda item: item["name"].casefold())


def schedule_dose_quantities(schedule: MedicationSchedule | None) -> dict[str, float]:
    """Read per-slot units while preserving one-unit legacy plans."""
    if not schedule:
        return {}
    slots = json.loads(schedule.regular_times or "[]")
    try:
        saved = json.loads(schedule.regular_doses or "{}")
    except (TypeError, json.JSONDecodeError):
        saved = {}
    return {slot: float(saved.get(slot, 1)) for slot in slots}


@app.put("/api/v1/products/{product_id}/common-name")
def update_common_name(
    product_id: int,
    payload: CommonNameUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    owned = (
        session.query(Pack).filter(accessible_pack_filter(user, session), Pack.product_id == product_id).first()
        or session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first()
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Medicine not found")
    common_name = payload.common_name.strip()
    record = session.query(ProductCommonName).filter_by(user_id=user.id, product_id=product_id).first()
    if record:
        record.common_name = common_name
    else:
        session.add(ProductCommonName(user_id=user.id, product_id=product_id, common_name=common_name))
    session.commit()
    return {"common_name": common_name}


@app.get("/api/v1/households", response_model=list[HouseholdRead])
def list_households(user: User = Depends(current_user), session: Session = Depends(get_session)):
    memberships = session.query(HouseholdMembership).filter_by(user_id=user.id).all()
    result = []
    for membership in memberships:
        household = session.get(Household, membership.household_id)
        if household:
            result.append({
                "id": household.id,
                "name": household.name,
                "role": membership.role,
                "member_count": session.query(HouseholdMembership).filter_by(household_id=household.id).count(),
            })
    return result


@app.post("/api/v1/households", response_model=HouseholdRead, status_code=201)
def create_household(payload: HouseholdCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    household = Household(name=payload.name.strip(), created_by_user_id=user.id)
    session.add(household)
    session.flush()
    session.add(HouseholdMembership(household_id=household.id, user_id=user.id, role="admin"))
    session.commit()
    return {"id": household.id, "name": household.name, "role": "admin", "member_count": 1}


@app.post("/api/v1/households/{household_id}/members", response_model=HouseholdRead)
def add_household_member(
    household_id: int,
    payload: HouseholdMemberCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    require_household_role(user, household_id, "admin", session)
    member = session.query(User).filter_by(email=payload.email.strip().lower()).first()
    if not member:
        raise HTTPException(status_code=404, detail="That person needs a DoseKeep account before they can be added")
    record = session.query(HouseholdMembership).filter_by(household_id=household_id, user_id=member.id).first()
    if record:
        record.role = payload.role
    else:
        session.add(HouseholdMembership(household_id=household_id, user_id=member.id, role=payload.role))
    session.commit()
    household = session.get(Household, household_id)
    return {"id": household.id, "name": household.name, "role": household_role(user, household_id, session), "member_count": session.query(HouseholdMembership).filter_by(household_id=household_id).count()}


@app.get("/api/v1/households/{household_id}/guest-links")
def list_cabinet_guest_links(household_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_household_role(user, household_id, "admin", session)
    result = []
    for item in session.query(CabinetGuestLink).filter_by(household_id=household_id).order_by(CabinetGuestLink.created_at.desc()).all():
        url = cabinet_guest_url(item) if not item.revoked_at else None
        result.append({
            "id": item.id,
            "label": item.label,
            "require_name": item.require_name,
            "revoked_at": item.revoked_at,
            "created_at": item.created_at,
            "url": url,
            "qr_data_url": cabinet_qr_data_url(url) if url else None,
        })
    return result


@app.post("/api/v1/households/{household_id}/guest-links", status_code=201)
def create_cabinet_guest_link(
    household_id: int,
    payload: CabinetGuestLinkCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    require_household_role(user, household_id, "admin", session)
    token = secrets.token_urlsafe(32)
    record = CabinetGuestLink(
        household_id=household_id,
        label=payload.label.strip(),
        require_name=payload.require_name,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        encrypted_token=encrypt_config({"token": token}),
    )
    session.add(record)
    session.commit()
    url = cabinet_guest_url(record)
    return {
        "id": record.id,
        "label": record.label,
        "require_name": record.require_name,
        "url": url,
        "qr_data_url": cabinet_qr_data_url(url),
    }


@app.delete("/api/v1/households/{household_id}/guest-links/{link_id}")
def revoke_cabinet_guest_link(household_id: int, link_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_household_role(user, household_id, "admin", session)
    record = session.query(CabinetGuestLink).filter_by(id=link_id, household_id=household_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Guest link not found")
    record.revoked_at = datetime.utcnow()
    session.commit()
    return {"ok": True}


@app.get("/api/v1/households/{household_id}/invites")
def list_household_invites(household_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_household_role(user, household_id, "admin", session)
    return [
        {
            "id": item.id,
            "role": item.role,
            "add_to_household": item.add_to_household,
            "accepted_at": item.accepted_at,
            "revoked_at": item.revoked_at,
            "created_at": item.created_at,
            "url": household_invite_url(item) if not item.revoked_at and not item.accepted_at else None,
        }
        for item in session.query(HouseholdInvite).filter_by(household_id=household_id).order_by(HouseholdInvite.created_at.desc()).all()
    ]


@app.post("/api/v1/households/{household_id}/invites", status_code=201)
def create_household_invite(
    household_id: int,
    payload: HouseholdInviteCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    require_household_role(user, household_id, "admin", session)
    token = secrets.token_urlsafe(32)
    record = HouseholdInvite(
        household_id=household_id,
        created_by_user_id=user.id,
        add_to_household=payload.add_to_household,
        role=payload.role,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        encrypted_token=encrypt_config({"token": token}),
    )
    session.add(record)
    session.commit()
    return {"id": record.id, "url": household_invite_url(record), "add_to_household": record.add_to_household, "role": record.role}


@app.delete("/api/v1/households/{household_id}/invites/{invite_id}")
def revoke_household_invite(household_id: int, invite_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_household_role(user, household_id, "admin", session)
    record = session.query(HouseholdInvite).filter_by(id=invite_id, household_id=household_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Household invitation not found")
    record.revoked_at = datetime.utcnow()
    session.commit()
    return {"ok": True}


@app.get("/invite/{token}", response_class=HTMLResponse)
def accept_household_invite(token: str, request: Request, session: Session = Depends(get_session)):
    record = session.query(HouseholdInvite).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not record or record.revoked_at or record.accepted_at:
        raise HTTPException(status_code=404, detail="This invitation is no longer available")
    request.session["household_invite_id"] = record.id
    household = session.get(Household, record.household_id) if record.add_to_household else None
    destination = f"the {html.escape(household.name)} household as {html.escape(record.role)}" if household else "DoseKeep"
    return HTMLResponse(f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DoseKeep invitation</title><style>body{{font-family:system-ui,sans-serif;max-width:36rem;padding:2rem;margin:auto;color:#173222}}a{{display:block;padding:.9rem;border-radius:.55rem;background:#166534;color:#fff;font-weight:700;text-align:center;text-decoration:none}}</style><h1>You’re invited to DoseKeep</h1><p>Create an account or sign in to accept access to {destination}.</p><a href="/">Continue to DoseKeep</a></html>''')


def cabinet_actor_name(request: Request, link: CabinetGuestLink, session: Session) -> str | None:
    return request.session.get(f"cabinet_actor_{link.id}")


@app.get("/cabinet/{token}", response_class=HTMLResponse)
def cabinet_guest_page(token: str, request: Request, session: Session = Depends(get_session)):
    link = cabinet_guest_link(token, session)
    household = session.get(Household, link.household_id)
    actor = cabinet_actor_name(request, link, session)
    signed_in_user = session.get(User, request.session.get("user_id"))
    display_names = household_display_names(household, session)
    packs = session.query(Pack).filter_by(household_id=link.household_id, status="active").filter(Pack.quantity_remaining > 0).all()
    totals: dict[int, int] = {}
    for pack in packs:
        totals[pack.product_id] = totals.get(pack.product_id, 0) + pack.quantity_remaining
    items = []
    for product_id, quantity in totals.items():
        product = session.get(Product, product_id)
        name = html.escape(display_names.get(product_id) or (product.name if product else "Medicine"))
        action = f'<button data-product-id="{product_id}">Record one used</button>' if actor else ""
        items.append(f"<section><h2>{name}</h2><p>{quantity} item{'s' if quantity != 1 else ''} available</p>{action}</section>")
    content = "".join(items) or "<p>No cabinet stock is currently recorded.</p>"
    required = "required" if link.require_name else ""
    account_choice = (
        f'<button type="button" id="account-choice">Continue as {html.escape(signed_in_user.email)}</button>'
        if signed_in_user
        else '<p>Already registered? <a href="/">Sign in to DoseKeep</a>, then return to this QR link.</p>'
    )
    identity = (
        f'<p>Recording as <strong>{html.escape(actor)}</strong>. <button type="button" id="change-identity">Change name / identity</button></p>'
        if actor
        else f'''{account_choice}<form id="guest-form"><label for="guest-name">Your name{' (required)' if link.require_name else ' (optional)'}</label><input id="guest-name" maxlength="120" {required} placeholder="Name for the cabinet record" /><button>Continue as guest</button></form>'''
    )
    return HTMLResponse(
        f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(household.name)} cabinet</title>
<style>body{{font-family:system-ui,sans-serif;background:#f7f8f4;color:#173222;margin:0;padding:1.25rem;max-width:38rem}}section,form{{background:#fff;padding:1.1rem;margin:1rem 0;border-radius:1rem;box-shadow:0 1px 3px #0002}}h1,h2{{margin:.1rem 0 .5rem}}p{{color:#475569}}input,button{{width:100%;box-sizing:border-box;margin-top:.6rem;padding:.8rem;border-radius:.55rem;font:inherit}}input{{border:1px solid #9ca3af}}button{{border:0;background:#166534;color:white;font-weight:700}}#result{{font-weight:700}}</style>
<h1>{html.escape(household.name)} cabinet</h1><p>Shared cabinet access</p>{identity}<div id="items">{content}</div><p id="result"></p>
<script>const result=document.querySelector('#result');async function identity(body){{const response=await fetch(location.pathname+'/guest',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});if(response.ok)location.reload();else result.textContent=(await response.json()).detail||'Could not continue';}}const guest=document.querySelector('#guest-form');if(guest)guest.addEventListener('submit',event=>{{event.preventDefault();identity({{mode:'guest',name:document.querySelector('#guest-name').value}});}});const account=document.querySelector('#account-choice');if(account)account.addEventListener('click',()=>identity({{mode:'account'}}));const change=document.querySelector('#change-identity');if(change)change.addEventListener('click',()=>identity({{mode:'clear'}}));document.querySelectorAll('[data-product-id]').forEach(button=>button.addEventListener('click',async()=>{{button.disabled=true;const response=await fetch(location.pathname+'/record',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{product_id:Number(button.dataset.productId)}})}});const data=await response.json();if(response.ok){{result.textContent=`Recorded: ${{data.product_name}}.`;location.reload();}}else{{result.textContent=data.detail||'Could not record use';button.disabled=false;}}}}));</script></html>'''
    )


@app.post("/cabinet/{token}/guest")
async def identify_cabinet_guest(token: str, request: Request, session: Session = Depends(get_session)):
    link = cabinet_guest_link(token, session)
    try:
        body = await request.json()
        mode = body.get("mode", "guest")
        name = str(body.get("name", "")).strip()
    except Exception:
        mode = "guest"
        name = ""
    if mode == "clear":
        request.session.pop(f"cabinet_actor_{link.id}", None)
        return {"ok": True}
    if mode == "account":
        user = session.get(User, request.session.get("user_id"))
        if not user:
            raise HTTPException(status_code=401, detail="Sign in before selecting your account")
        name = user.email
    if link.require_name and not name:
        raise HTTPException(status_code=422, detail="Enter your name to use this cabinet")
    request.session[f"cabinet_actor_{link.id}"] = name or "Guest"
    return {"ok": True}


@app.post("/cabinet/{token}/record")
async def record_cabinet_guest_use(token: str, request: Request, session: Session = Depends(get_session)):
    link = cabinet_guest_link(token, session)
    actor = cabinet_actor_name(request, link, session)
    if not actor:
        raise HTTPException(status_code=403, detail="Choose guest access or sign in first")
    try:
        product_id = int((await request.json()).get("product_id"))
    except Exception:
        raise HTTPException(status_code=422, detail="Choose a cabinet item")
    pack = session.query(Pack).filter_by(household_id=link.household_id, product_id=product_id, status="active").filter(Pack.quantity_remaining > 0).order_by(Pack.expiry_date.is_(None), Pack.expiry_date, Pack.id).first()
    if not pack:
        raise HTTPException(status_code=409, detail="That item is no longer available")
    pack.quantity_remaining -= 1
    session.add(SupplyEvent(pack_id=pack.id, event_type="taken_from_pack", quantity=1, notes="Shared cabinet use", actor_name=actor))
    session.commit()
    product = session.get(Product, product_id)
    household = session.get(Household, link.household_id)
    return {"ok": True, "product_name": household_display_names(household, session).get(product_id) or (product.name if product else "Item")}


@app.put("/api/v1/packs/{pack_id}/household", response_model=PackRead)
def set_pack_household(
    pack_id: int,
    payload: PackHouseholdUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    pack = session.query(Pack).filter(Pack.id == pack_id, accessible_pack_filter(user, session)).first()
    if not pack or not can_change_pack(user, pack, session):
        raise HTTPException(status_code=404, detail="Pack not found")
    if payload.household_id:
        require_household_role(user, payload.household_id, "contributor", session)
    pack.household_id = payload.household_id
    session.commit()
    session.refresh(pack)
    return pack


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
    owns_product = session.query(Pack).filter(accessible_pack_filter(user, session), Pack.product_id == product_id).first()
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
    regular_times = sorted(set(payload.regular_times))
    schedule.regular_times = json.dumps(regular_times)
    schedule.regular_doses = json.dumps({slot: payload.dose_quantities.get(slot, 1) for slot in regular_times})
    schedule.as_required = payload.as_required
    schedule.prn_notes = payload.prn_notes.strip() if payload.prn_notes else None
    # Retire today’s still-pending doses for slots removed from this plan.
    # Without this, moving a medicine from bedtime to evening leaves both
    # records visible until the next day.
    zone = user_zone(user)
    today = datetime.now(timezone.utc).astimezone(zone).date()
    day_start = datetime.combine(today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    day_end = day_start + timedelta(days=1)
    active_slots = set(payload.regular_times)
    stale_doses = session.query(ScheduledDose).filter(
        ScheduledDose.user_id == user.id,
        ScheduledDose.product_id.in_(related_product_ids),
        ScheduledDose.scheduled_for >= day_start,
        ScheduledDose.scheduled_for < day_end,
        ScheduledDose.administration_time.in_(list(DEFAULT_SLOT_TIMES)),
        ScheduledDose.status.in_(("due", "snoozed")),
    )
    if active_slots:
        stale_doses = stale_doses.filter(~ScheduledDose.administration_time.in_(active_slots))
    stale_doses.delete(synchronize_session=False)
    session.commit()
    return next(item for item in list_medicine_dashboard(user, session) if item["product_id"] == product_id)


def user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "Europe/Paris")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Paris")


def generate_today_doses(user: User, session: Session) -> None:
    """Materialise today’s regular plan as actionable doses, idempotently."""
    zone = user_zone(user)
    local_today = datetime.now(timezone.utc).astimezone(zone).date()
    day_start = datetime.combine(local_today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    day_end = day_start + timedelta(days=1)
    all_by_id = {}
    try:
        all_by_id = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        pass
    links = {item.product_id: item for item in session.query(MediKeepLink).filter_by(user_id=user.id).all()}
    schedules = session.query(MedicationSchedule).filter_by(user_id=user.id).all()
    slot_times = administration_times(user)
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
                # A dose may have been generated before MediKeep was refreshed.
                # Remove only today's unactioned entries: actual historic MAR
                # actions remain intact, while a stopped medicine is not shown
                # as overdue or missed.
                removed = session.query(ScheduledDose).filter(
                    ScheduledDose.user_id == user.id,
                    ScheduledDose.product_id == schedule.product_id,
                    ScheduledDose.scheduled_for >= day_start,
                    ScheduledDose.scheduled_for < day_end,
                    ScheduledDose.status.in_(("due", "snoozed")),
                ).delete(synchronize_session=False)
                if removed:
                    generated = True
                continue
        dose_quantities = schedule_dose_quantities(schedule)
        scheduled_slots = set(dose_quantities)
        # Also clear old pending records when a plan was changed before this
        # cleanup was available. This repairs today's duplicate slot on load.
        stale_doses = session.query(ScheduledDose).filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.product_id == schedule.product_id,
            ScheduledDose.scheduled_for >= day_start,
            ScheduledDose.scheduled_for < day_end,
            ScheduledDose.administration_time.in_(list(DEFAULT_SLOT_TIMES)),
            ScheduledDose.status.in_(("due", "snoozed")),
        )
        if scheduled_slots:
            stale_doses = stale_doses.filter(~ScheduledDose.administration_time.in_(scheduled_slots))
        if stale_doses.delete(synchronize_session=False):
            generated = True
        for slot in scheduled_slots:
            configured_time = slot_times.get(slot)
            if not configured_time:
                continue
            clock = time.fromisoformat(configured_time)
            local_due = datetime.combine(local_today, clock, tzinfo=zone)
            due = local_due.astimezone(timezone.utc).replace(tzinfo=None)
            exists = session.query(ScheduledDose).filter_by(
                user_id=user.id,
                product_id=schedule.product_id,
                administration_time=slot,
                scheduled_for=due,
            ).first()
            if not exists:
                # If the user changed a slot after today's dose was created,
                # move the still-pending record instead of displaying both
                # the old time and the newly configured one.
                exists = (
                    session.query(ScheduledDose)
                    .filter(
                        ScheduledDose.user_id == user.id,
                        ScheduledDose.product_id == schedule.product_id,
                        ScheduledDose.administration_time == slot,
                        ScheduledDose.scheduled_for >= day_start,
                        ScheduledDose.scheduled_for < day_end,
                        ScheduledDose.status == "due",
                    )
                    .first()
                )
                if exists:
                    exists.scheduled_for = due
                    exists.due_at = due
                    exists.quantity = dose_quantities[slot]
                    exists.notified_at = None
                    generated = True
            elif exists.status in {"due", "snoozed"} and exists.quantity != dose_quantities[slot]:
                # A still-pending dose follows an edited plan. Once taken or
                # skipped, the stored quantity remains its MAR snapshot.
                exists.quantity = dose_quantities[slot]
                generated = True
            if not exists:
                session.add(
                    ScheduledDose(
                        user_id=user.id,
                        product_id=schedule.product_id,
                        administration_time=slot,
                        scheduled_for=due,
                        due_at=due,
                        quantity=dose_quantities[slot],
                    )
                )
                generated = True
    if generated:
        session.commit()
    reconcile_duplicate_today_doses(user, session, day_start, day_end, zone)


def reconcile_duplicate_today_doses(
    user: User,
    session: Session,
    day_start: datetime,
    day_end: datetime,
    zone: ZoneInfo,
) -> None:
    """Hide an old pending MAR row when the same scheduled dose was recorded.

    Product records created before MediKeep links were made stable can leave a
    second, unlinked scheduled dose behind.  It is not another administration
    opportunity, but previously it could still produce an ntfy reminder after
    the current medicine had been recorded as taken.
    """
    doses = (
        session.query(ScheduledDose)
        .filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.scheduled_for >= day_start,
            ScheduledDose.scheduled_for < day_end,
            ScheduledDose.administration_time.in_(list(DEFAULT_SLOT_TIMES)),
        )
        .all()
    )
    if not doses:
        return
    product_ids = {dose.product_id for dose in doses}
    products = {product.id: product for product in session.query(Product).filter(Product.id.in_(product_ids)).all()}
    names = medication_display_names(user, session)
    links_by_product = {
        link.product_id: link
        for link in session.query(MediKeepLink).filter_by(user_id=user.id).all()
    }
    active_product_ids = {
        product_id
        for product_id, in session.query(Pack.product_id)
        .filter(accessible_pack_filter(user, session), Pack.status == "active")
        .distinct()
        .all()
    }
    linked_keys_by_identity: dict[frozenset[str], set[str]] = {}
    for product_id, link in links_by_product.items():
        product = products.get(product_id) or session.get(Product, product_id)
        if not product:
            continue
        identity = frozenset(normalize_name(names.get(product_id) or product.name))
        if identity:
            linked_keys_by_identity.setdefault(identity, set()).add(f"medikeep:{link.medikeep_medication_id}")

    def dose_key(dose: ScheduledDose) -> tuple[str, bool]:
        link = links_by_product.get(dose.product_id)
        if link:
            return f"medikeep:{link.medikeep_medication_id}", False
        # An unlinked product with no active pack may only be historic MAR
        # debris.  Match it only when its displayed name maps unambiguously to
        # one linked medicine; never guess between similarly named medicines.
        product = products.get(dose.product_id)
        if product and dose.product_id not in active_product_ids:
            candidates = linked_keys_by_identity.get(
                frozenset(normalize_name(names.get(dose.product_id) or product.name)), set()
            )
            if len(candidates) == 1:
                return next(iter(candidates)), True
        return f"product:{dose.product_id}", False

    recorded_slots = {
        (
            dose_key(dose)[0],
            dose.scheduled_for.replace(tzinfo=timezone.utc).astimezone(zone).date(),
            dose.administration_time,
        )
        for dose in doses
        if dose.status in {"taken", "skipped"}
    }
    changed = False
    for dose in doses:
        if dose.status not in {"due", "snoozed"}:
            continue
        key, orphan = dose_key(dose)
        slot_key = (
            key,
            dose.scheduled_for.replace(tzinfo=timezone.utc).astimezone(zone).date(),
            dose.administration_time,
        )
        # Exact same medicine/slot duplicates are always invalid.  For an
        # orphan product, require the unambiguous identity check above.
        if slot_key in recorded_slots and (orphan or key.startswith("medikeep:") or key.startswith("product:")):
            dose.status = "superseded"
            dose.notes = "Superseded duplicate scheduled dose"
            changed = True
    if changed:
        session.commit()


def generate_regular_doses_for_date(user: User, session: Session, local_date: date) -> None:
    """Materialise a future day's regular plan so it can be put in a pill box.

    Unlike the daily generator this deliberately does not rewrite a future
    record after a plan edit: once someone has physically prepared a dose, its
    time and quantity are an audit snapshot.
    """
    zone = user_zone(user)
    all_by_id = {}
    try:
        all_by_id = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        pass
    links = {item.product_id: item for item in session.query(MediKeepLink).filter_by(user_id=user.id).all()}
    slot_times = administration_times(user)
    seen_medikeep_ids = set()
    created = False
    for schedule in session.query(MedicationSchedule).filter_by(user_id=user.id).all():
        link = links.get(schedule.product_id)
        if link:
            if link.medikeep_medication_id in seen_medikeep_ids:
                continue
            seen_medikeep_ids.add(link.medikeep_medication_id)
            external = all_by_id.get(link.medikeep_medication_id)
            if external and external.status != "active":
                continue
        for slot, quantity in schedule_dose_quantities(schedule).items():
            configured_time = slot_times.get(slot)
            if not configured_time:
                continue
            due = datetime.combine(local_date, time.fromisoformat(configured_time), tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
            exists = session.query(ScheduledDose).filter_by(
                user_id=user.id, product_id=schedule.product_id,
                administration_time=slot, scheduled_for=due,
            ).first()
            if not exists:
                session.add(ScheduledDose(
                    user_id=user.id, product_id=schedule.product_id,
                    administration_time=slot, scheduled_for=due,
                    due_at=due, quantity=quantity,
                ))
                created = True
    if created:
        session.flush()


def medication_display_names(user: User, session: Session) -> dict[int, str]:
    """Return preferred common names, with MediKeep and pack-name fallbacks.

    DoseKeep works alone: a user-edited/common catalogue name wins. Linked
    MediKeep names remain the fallback for older linked products.
    """
    names = user_common_names(user, session)
    try:
        medicines = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        return names
    for link in session.query(MediKeepLink).filter_by(user_id=user.id).all():
        if medicine := medicines.get(link.medikeep_medication_id):
            names.setdefault(link.product_id, medicine.name)
    return names


def dose_read(dose: ScheduledDose, session: Session, display_names: dict[int, str] | None = None) -> dict:
    product = session.get(Product, dose.product_id)
    stock = sum(
        pack.quantity_remaining + pack.quantity_in_dosette
        for pack in session.query(Pack).filter(accessible_pack_filter(dose.user, session), Pack.product_id == dose.product_id, Pack.status == "active").all()
    )
    return {
        "id": dose.id,
        "product_id": dose.product_id,
        "medicine_name": (display_names or {}).get(dose.product_id) or (product.name if product else "Unknown medicine"),
        "dosage": product.strength if product else None,
        "route": None,
        "administration_time": dose.administration_time,
        "scheduled_for": dose.scheduled_for,
        "due_at": dose.due_at,
        "status": dose.status,
        "quantity": dose.quantity,
        "prepared_at": dose.prepared_at,
        "actioned_at": dose.actioned_at,
        "notes": dose.notes,
        "stock_available": stock,
    }


def notification_action_url(user: User, doses: list[ScheduledDose], now: datetime, session: Session) -> str:
    """Create an unguessable link limited to this reminder's doses until 04:00."""
    zone = user_zone(user)
    local_tomorrow = now.replace(tzinfo=timezone.utc).astimezone(zone).date() + timedelta(days=1)
    expires_at = datetime.combine(local_tomorrow, time(hour=4), tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    token = secrets.token_urlsafe(32)
    session.query(NotificationActionLink).filter(NotificationActionLink.expires_at < now).delete(synchronize_session=False)
    session.add(
        NotificationActionLink(
            user_id=user.id,
            dose_ids=json.dumps([dose.id for dose in doses]),
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=expires_at,
        )
    )
    return f"{os.getenv('DOSEKEEP_PUBLIC_URL', 'https://dosekeep.godsil.co.uk').rstrip('/')}/dose-actions/{token}"


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def pill_box_cycle_requirements(user: User, settings: dict, cycle_start: date, session: Session) -> dict[int, float]:
    """Units needed for one configured box cycle, grouped by medicine."""
    selected_dates = [
        cycle_start + timedelta(days=offset)
        for offset in range(settings["days"])
        if (cycle_start + timedelta(days=offset)).weekday() in set(settings["weekdays"])
    ]
    selected_slots = set(settings["slots"])
    links = {item.product_id: item for item in session.query(MediKeepLink).filter_by(user_id=user.id).all()}
    all_by_id = {}
    try:
        all_by_id = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
    except (MediKeepUnavailable, HTTPException):
        pass
    seen_medikeep_ids = set()
    requirements: dict[int, float] = {}
    for schedule in session.query(MedicationSchedule).filter_by(user_id=user.id).all():
        link = links.get(schedule.product_id)
        if link:
            if link.medikeep_medication_id in seen_medikeep_ids:
                continue
            seen_medikeep_ids.add(link.medikeep_medication_id)
            external = all_by_id.get(link.medikeep_medication_id)
            if external and external.status != "active":
                continue
        daily = sum(quantity for slot, quantity in schedule_dose_quantities(schedule).items() if slot in selected_slots)
        if daily:
            requirements[schedule.product_id] = daily * len(selected_dates)
    return requirements


def process_pill_box_routine(user: User, settings: dict, now: datetime, session: Session) -> bool:
    """Send one weekly top-up prompt and one configurable two-box stock check."""
    routine = pill_box_settings(user)
    if not routine["enabled"]:
        return False
    zone = user_zone(user)
    today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    today_key = today.isoformat()
    changed = False
    public_url = os.getenv("DOSEKEEP_PUBLIC_URL", "https://dosekeep.godsil.co.uk").rstrip("/") + "/#administration"
    raw = dict(routine)
    if today.weekday() == routine["top_up_weekday"] and raw.get("top_up_notified_for") != today_key:
        title = "DoseKeep: pill-box top-up due"
        message = f"Prepare your {routine['days']}-day pill box for {WEEKDAY_NAMES[routine['top_up_weekday']]} onward."
        try:
            send_ntfy(settings["server_url"], settings["topic"], title, message, "pill,calendar", public_url)
        except Exception as error:
            print(f"ntfy pill-box top-up reminder failed for user {user.id}: {error}")
        else:
            raw["top_up_notified_for"] = today_key
            changed = True
    if today.weekday() == routine["stock_check_weekday"] and raw.get("stock_check_notified_for") != today_key:
        days_until_topup = (routine["top_up_weekday"] - today.weekday()) % 7
        next_topup = today + timedelta(days=days_until_topup)
        first = pill_box_cycle_requirements(user, routine, next_topup, session)
        second = pill_box_cycle_requirements(user, routine, next_topup + timedelta(days=routine["days"]), session)
        names = medication_display_names(user, session)
        warnings = []
        for product_id in set(first) | set(second):
            target = first.get(product_id, 0) + second.get(product_id, 0)
            available = sum(
                pack.quantity_remaining + pack.quantity_in_dosette
                for pack in session.query(Pack).filter(accessible_pack_filter(user, session), Pack.product_id.in_(related_product_ids(user, product_id, session)), Pack.status == "active").all()
            )
            if available + 1e-9 < target:
                product = session.get(Product, product_id)
                medicine = names.get(product_id) or (product.name if product else "Medicine")
                warnings.append(f"{medicine}: {available:g} available; {target:g} needed for this and next box — add at least {target - available:g}")
        if warnings:
            try:
                send_ntfy(settings["server_url"], settings["topic"], "DoseKeep: pill-box stock warning", " · ".join(warnings), "warning,pill", public_url)
            except Exception as error:
                print(f"ntfy pill-box stock warning failed for user {user.id}: {error}")
            else:
                raw["stock_check_notified_for"] = today_key
                changed = True
        else:
            # Mark a clear weekly check as done too; otherwise the worker
            # would recalculate it every minute.
            raw["stock_check_notified_for"] = today_key
            changed = True
    if changed:
        user.pill_box_settings = json.dumps(raw)
    return changed


def process_due_notifications(session: Session) -> None:
    """Generate due doses and send each configured ntfy reminder once."""
    now = datetime.utcnow()
    changed = False
    for user in session.query(User).all():
        settings = notification_settings(user)
        if not settings["enabled"] or not settings["topic"]:
            continue
        # Pill-box top-up/stock prompts are useful extras, but they must never
        # suppress the core scheduled-dose reminder pass.  In particular, a
        # malformed historic routine setting or a routine-only calculation
        # error should leave normal medication reminders working.
        try:
            changed = process_pill_box_routine(user, settings, now, session) or changed
        except Exception as error:
            print(f"pill-box routine failed for user {user.id}; continuing with medication reminders: {error}")
        generate_today_doses(user, session)
        display_names = medication_display_names(user, session)
        zone = user_zone(user)
        local_today = datetime.now(timezone.utc).astimezone(zone).date()
        day_start = datetime.combine(local_today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        pending = (
            session.query(ScheduledDose)
            .filter(
                ScheduledDose.user_id == user.id,
                ScheduledDose.status.in_(("due", "snoozed", "prepared")),
                ScheduledDose.scheduled_for >= day_start,
                ScheduledDose.due_at <= now,
                ScheduledDose.notified_at.is_(None),
            )
            .order_by(ScheduledDose.due_at)
            .all()
        )
        batches: dict[tuple[str, datetime], list[ScheduledDose]] = {}
        for dose in pending:
            # Regular doses at a slot share the same due_at, yielding one
            # useful morning/evening notification. Snoozed doses acquire a
            # new due_at and are therefore reminded separately later.
            batches.setdefault((dose.administration_time, dose.due_at), []).append(dose)
        for (administration_time, _), doses in batches.items():
            details = []
            has_no_stock = False
            for dose in doses:
                product = session.get(Product, dose.product_id)
                medicine = display_names.get(dose.product_id) or (product.name if product else "your medicine")
                stock = sum(
                    pack.quantity_remaining + pack.quantity_in_dosette
                    for pack in session.query(Pack).filter(accessible_pack_filter(user, session), Pack.product_id == dose.product_id, Pack.status == "active").all()
                )
                if stock <= 0:
                    details.append(f"{medicine} ({dose.quantity:g} item{'s' if dose.quantity != 1 else ''}; no stock recorded)")
                    has_no_stock = True
                else:
                    details.append(f"{medicine} ({dose.quantity:g} item{'s' if dose.quantity != 1 else ''}; {stock:g} available)")
            slot = administration_time.title()
            prepared_count = sum(1 for dose in doses if dose.prepared_at)
            if prepared_count == len(doses):
                title = f"DoseKeep: prepared {slot} pill box due"
                tags = "pill,calendar"
            elif has_no_stock:
                title = f"DoseKeep: {slot} medicines due — stock warning"
                tags = "warning,pill"
            else:
                title = f"DoseKeep: {slot} medicines due"
                tags = "pill"
            prepared_note = f" {prepared_count} dose{'s' if prepared_count != 1 else ''} prepared in your pill box." if prepared_count else ""
            message = f"{' · '.join(details)}.{prepared_note} Open this notification to record it."
            action_url = notification_action_url(user, doses, now, session)
            try:
                send_ntfy(settings["server_url"], settings["topic"], title, message, tags, action_url)
            except Exception as error:
                # Keep the dose unmarked for a later retry.
                print(f"ntfy reminder failed for user {user.id}, {administration_time} batch: {error}")
                continue
            for dose in doses:
                dose.notified_at = now
            changed = True
    if changed:
        session.commit()


def _run_notification_cycle() -> None:
    with SessionLocal() as session:
        process_due_notifications(session)


async def notification_worker() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_notification_cycle)
        except Exception as error:
            print(f"ntfy reminder worker failed: {error}")
        await asyncio.sleep(60)


def related_product_ids(user: User, product_id: int, session: Session) -> list[int]:
    """Include historic pack products linked to the same MediKeep medicine."""
    link = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product_id).first()
    if not link:
        return [product_id]
    return [
        item.product_id
        for item in session.query(MediKeepLink)
        .filter_by(user_id=user.id, medikeep_medication_id=link.medikeep_medication_id)
        .all()
    ]


def stock_packs(user: User, product_id: int, session: Session) -> list[Pack]:
    product_ids = related_product_ids(user, product_id, session)
    return (
        session.query(Pack)
        .filter(accessible_pack_filter(user, session), Pack.product_id.in_(product_ids), Pack.status == "active", Pack.quantity_remaining > 0)
        .order_by(Pack.expiry_date.is_(None), Pack.expiry_date, Pack.id)
        .all()
    )


def oldest_stock_pack(user: User, product_id: int, session: Session) -> Pack | None:
    return next(iter(stock_packs(user, product_id, session)), None)


def consume_stock(user: User, product_id: int, quantity: float, session: Session, notes: str) -> None:
    """Deduct a dose across packs in expiry order, including fractional units."""
    packs = stock_packs(user, product_id, session)
    if sum(pack.quantity_remaining for pack in packs) + 1e-9 < quantity:
        raise HTTPException(status_code=409, detail="No recorded pack has enough stock for this dose")
    remaining = quantity
    for pack in packs:
        used = min(pack.quantity_remaining, remaining)
        if used <= 0:
            continue
        pack.quantity_remaining = round(pack.quantity_remaining - used, 6)
        session.add(SupplyEvent(pack_id=pack.id, event_type="taken_from_pack", quantity=used, notes=notes))
        remaining = round(remaining - used, 6)
        if remaining <= 1e-9:
            break


def fill_pill_box(user: User, dose: ScheduledDose, session: Session, notes: str) -> None:
    """Move units from packs into the physical pill-box allocation."""
    packs = stock_packs(user, dose.product_id, session)
    if sum(pack.quantity_remaining for pack in packs) + 1e-9 < dose.quantity:
        raise HTTPException(status_code=409, detail="No recorded pack has enough stock to prepare this dose")
    remaining = dose.quantity
    for pack in packs:
        moved = min(pack.quantity_remaining, remaining)
        if moved <= 0:
            continue
        pack.quantity_remaining = round(pack.quantity_remaining - moved, 6)
        pack.quantity_in_dosette = round(pack.quantity_in_dosette + moved, 6)
        session.add(SupplyEvent(pack_id=pack.id, event_type="dosette_fill", quantity=moved, notes=notes))
        session.add(PillBoxAllocation(dose_id=dose.id, pack_id=pack.id, quantity=moved))
        remaining = round(remaining - moved, 6)
        if remaining <= 1e-9:
            break


def prepared_allocations(dose: ScheduledDose, session: Session) -> list[PillBoxAllocation]:
    return session.query(PillBoxAllocation).filter_by(dose_id=dose.id, status="prepared").order_by(PillBoxAllocation.id).all()


def consume_pill_box(dose: ScheduledDose, session: Session, notes: str) -> None:
    """Record a prepared dose as taken, using the physical pill-box stock."""
    allocations = prepared_allocations(dose, session)
    if sum(item.quantity for item in allocations) + 1e-9 < dose.quantity:
        raise HTTPException(status_code=409, detail="The prepared pill-box count is too low for this dose; correct the physical count first")
    for allocation in allocations:
        pack = session.get(Pack, allocation.pack_id)
        if not pack or pack.quantity_in_dosette + 1e-9 < allocation.quantity:
            raise HTTPException(status_code=409, detail="The prepared pill-box count is too low for this dose; correct the physical count first")
        pack.quantity_in_dosette = round(pack.quantity_in_dosette - allocation.quantity, 6)
        allocation.status = "taken"
        allocation.resolved_at = datetime.utcnow()
        session.add(SupplyEvent(pack_id=pack.id, event_type="taken", quantity=allocation.quantity, notes=notes))


def record_taken_dose(dose: ScheduledDose, user: User, session: Session, now: datetime, notes: str | None = None) -> None:
    if dose.prepared_at:
        consume_pill_box(dose, session, notes or f"Prepared {dose.administration_time} dose")
    else:
        consume_stock(user, dose.product_id, dose.quantity, session, notes or f"Scheduled {dose.administration_time} dose")
    dose.status = "taken"
    dose.actioned_at = now
    dose.notes = notes


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
    display_names = medication_display_names(user, session)
    return [dose_read(dose, session, display_names) for dose in doses]


@app.get("/api/v1/pill-box/contents", response_model=list[ScheduledDoseRead])
def list_pill_box_contents(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """List doses which are still physically present in the pill box."""
    doses = (
        session.query(ScheduledDose)
        .filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.prepared_at.is_not(None),
            ScheduledDose.status.in_(("due", "snoozed", "prepared", "skipped")),
        )
        .order_by(ScheduledDose.scheduled_for, ScheduledDose.due_at, ScheduledDose.id)
        .all()
    )
    # A skipped prepared dose remains in the list only until it has been
    # returned to its pack or explicitly disposed of. Pending legacy prepared
    # records remain visible, even though they pre-date allocation tracking.
    visible = [
        dose for dose in doses
        if dose.status != "skipped" or prepared_allocations(dose, session)
    ]
    display_names = medication_display_names(user, session)
    return [dose_read(dose, session, display_names) for dose in visible]


@app.post("/api/v1/pill-box/prepare", response_model=PillBoxPrepareResult)
def prepare_pill_box(
    payload: PillBoxPrepareRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Prepare selected regular doses, stopping only where a dose lacks stock.

    Re-running the same selection is intentionally a safe top-up: already
    prepared doses are skipped and only the newly available ones are moved.
    """
    payload.validate_selection()
    selected_days = [
        payload.start_date + timedelta(days=offset)
        for offset in range(payload.days)
        if (payload.start_date + timedelta(days=offset)).weekday() in set(payload.weekdays)
    ]
    if not selected_days:
        raise HTTPException(status_code=422, detail="No preparation days fall within this range")
    for target in selected_days:
        generate_regular_doses_for_date(user, session, target)
    zone = user_zone(user)
    start = datetime.combine(min(selected_days), time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(max(selected_days) + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    doses = (
        session.query(ScheduledDose)
        .filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.scheduled_for >= start,
            ScheduledDose.scheduled_for < end,
            ScheduledDose.scheduled_for >= datetime.utcnow(),
            ScheduledDose.administration_time.in_(payload.slots),
            ScheduledDose.status == "due",
            ScheduledDose.prepared_at.is_(None),
        )
        .order_by(ScheduledDose.scheduled_for, ScheduledDose.due_at, ScheduledDose.id)
        .all()
    )
    names = medication_display_names(user, session)
    prepared_count = 0
    prepared_quantity = 0.0
    unavailable = []
    for dose in doses:
        try:
            local_when = dose.scheduled_for.replace(tzinfo=timezone.utc).astimezone(zone).strftime("%a %-d %b %H:%M")
            fill_pill_box(user, dose, session, f"Prepared for {local_when}")
            dose.prepared_at = datetime.utcnow()
            prepared_count += 1
            prepared_quantity += dose.quantity
        except HTTPException as error:
            product = session.get(Product, dose.product_id)
            medicine = names.get(dose.product_id) or (product.name if product else "Medicine")
            unavailable.append(f"{medicine} · {local_when}: {error.detail}")
    session.commit()
    return {
        "prepared_count": prepared_count,
        "prepared_quantity": prepared_quantity,
        "unavailable": unavailable,
    }


@app.post("/api/v1/pill-box/take", response_model=PillBoxTakeResult)
def take_prepared_doses(
    payload: PillBoxTakeRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Confirm a selected same-slot group of physically prepared doses."""
    doses = (
        session.query(ScheduledDose)
        .filter(ScheduledDose.user_id == user.id, ScheduledDose.id.in_(payload.dose_ids))
        .order_by(ScheduledDose.due_at, ScheduledDose.id)
        .all()
    )
    if len(doses) != len(set(payload.dose_ids)) or not all(dose.prepared_at for dose in doses):
        raise HTTPException(status_code=422, detail="Choose only prepared doses from your pill box")
    if len({dose.administration_time for dose in doses}) != 1:
        raise HTTPException(status_code=422, detail="Prepared doses must be from one administration time")
    take_prepared_dose_group(doses, user, session)
    return {"taken_count": len(doses)}


def take_prepared_dose_group(doses: list[ScheduledDose], user: User, session: Session) -> None:
    """Atomically record a prepared same-slot group as taken."""
    try:
        for dose in doses:
            apply_scheduled_dose_action(dose, user, ScheduledDoseAction(action="taken"), session)
        session.commit()
    except Exception:
        session.rollback()
        raise


def build_compliance_report(user: User, session: Session, days: int = 7) -> dict:
    """Summarise documented regular-dose outcomes for a recent period.

    PRN doses are deliberately excluded: they are not expected doses, so they
    would distort a regular-administration report. A currently snoozed dose is
    shown as pending rather than counted as missed.
    """
    if days not in {7, 30, 90}:
        raise HTTPException(status_code=422, detail="Choose a 7, 30 or 90 day report")
    # Use the same stable-medicine migration as the dashboard before looking
    # at historic MAR rows. Otherwise an old barcode/product record can show
    # as a duplicate medicine in compliance after a newer pack is linked.
    migrate_legacy_links(user, session)
    consolidate_medikeep_products(user, session)
    generate_today_doses(user, session)
    now = datetime.utcnow()
    zone = user_zone(user)
    local_today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    local_start = local_today - timedelta(days=days - 1)
    start = datetime.combine(local_start, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    doses = (
        session.query(ScheduledDose)
        .filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.administration_time.in_(list(DEFAULT_SLOT_TIMES)),
            ScheduledDose.scheduled_for >= start,
            ScheduledDose.scheduled_for <= now,
        )
        .order_by(ScheduledDose.scheduled_for.desc())
        .all()
    )
    names = medication_display_names(user, session)
    links_by_product = {
        link.product_id: link
        for link in session.query(MediKeepLink).filter_by(user_id=user.id).all()
    }
    products_by_id = {
        product.id: product
        for product in session.query(Product).filter(Product.id.in_({dose.product_id for dose in doses} | set(links_by_product))).all()
    }

    def report_identity(product_id: int) -> frozenset[str] | None:
        product = products_by_id.get(product_id)
        if not product:
            return None
        display_name = names.get(product_id) or product.name
        return frozenset(normalize_name(display_name))

    # A legacy product can survive solely in MAR history and therefore have no
    # link left to migrate. Do not broadly merge names: only use this fallback
    # for an orphan with no active accessible pack and exactly one linked
    # medicine sharing its displayed common name. Strength is intentionally
    # excluded: a generic common name can represent a brand/pack whose stored
    # catalogue strength is formatted differently from its replacement.
    active_product_ids = {
        product_id
        for product_id, in session.query(Pack.product_id)
        .filter(accessible_pack_filter(user, session), Pack.status == "active")
        .distinct()
        .all()
    }
    linked_keys_by_identity: dict[frozenset[str], set[str]] = {}
    for product_id, link in links_by_product.items():
        if identity := report_identity(product_id):
            linked_keys_by_identity.setdefault(identity, set()).add(f"medikeep:{link.medikeep_medication_id}")

    def report_medicine_key(product_id: int) -> tuple[str, bool]:
        """Return the report group and whether it is an orphan legacy record."""
        link = links_by_product.get(product_id)
        if link:
            return f"medikeep:{link.medikeep_medication_id}", False
        if product_id not in active_product_ids:
            candidates = linked_keys_by_identity.get(report_identity(product_id) or frozenset(), set())
            if len(candidates) == 1:
                return next(iter(candidates)), True
        return f"product:{product_id}", False

    # Reconcile a stale duplicate created by an old product record: if an
    # orphan pending record shares its local day and administration slot with
    # a documented dose for the single matched medicine, it was never an
    # additional administration opportunity. Preserve the row for audit, but
    # make its superseded status explicit so it is not counted as a miss.
    recorded_slots = {
        (
            report_medicine_key(dose.product_id)[0],
            dose.scheduled_for.replace(tzinfo=timezone.utc).astimezone(zone).date(),
            dose.administration_time,
        )
        for dose in doses
        if dose.status in {"taken", "skipped"}
    }
    reconciled = False
    for dose in doses:
        medicine_key, is_orphan = report_medicine_key(dose.product_id)
        slot_key = (
            medicine_key,
            dose.scheduled_for.replace(tzinfo=timezone.utc).astimezone(zone).date(),
            dose.administration_time,
        )
        if is_orphan and dose.status == "due" and slot_key in recorded_slots:
            dose.status = "superseded"
            dose.notes = "Superseded duplicate historical scheduled dose"
            reconciled = True
    if reconciled:
        session.commit()
    inactive_product_ids = set()
    try:
        current_medications = {item.id: item for item in all_medications(user_medikeep_config(user, session))}
        inactive_product_ids = {
            link.product_id
            for link in session.query(MediKeepLink).filter_by(user_id=user.id).all()
            if (medicine := current_medications.get(link.medikeep_medication_id)) and medicine.status != "active"
        }
    except (MediKeepUnavailable, HTTPException):
        pass
    totals = {"taken": 0, "skipped": 0, "missed": 0, "pending": 0}
    by_product: dict[str, dict] = {}
    exceptions = []
    for dose in doses:
        if dose.status == "superseded":
            continue
        # Retain real historical taken/skipped actions. Do not retroactively
        # count an unactioned scheduled dose as missed when MediKeep now says
        # that medicine is stopped.
        if dose.product_id in inactive_product_ids and dose.status in {"due", "snoozed"}:
            continue
        outcome = "missed"
        if dose.status == "taken":
            outcome = "taken"
        elif dose.status == "skipped":
            outcome = "skipped"
        elif dose.status == "snoozed" and dose.due_at > now:
            outcome = "pending"
        totals[outcome] += 1
        medicine_key, _ = report_medicine_key(dose.product_id)
        product = by_product.setdefault(
            medicine_key,
            {
                "product_id": dose.product_id,
                "medicine_name": names.get(dose.product_id) or (session.get(Product, dose.product_id).name if session.get(Product, dose.product_id) else "Unknown medicine"),
                "taken": 0,
                "skipped": 0,
                "missed": 0,
                "pending": 0,
                "history": [],
            },
        )
        product[outcome] += 1
        product["history"].append({
            "scheduled_for": dose.scheduled_for,
            "administration_time": dose.administration_time,
            "quantity": dose.quantity,
            "outcome": outcome,
            "actioned_at": dose.actioned_at,
        })
        if outcome in {"skipped", "missed"}:
            exceptions.append({
                "medicine_name": product["medicine_name"],
                "outcome": outcome,
                "scheduled_for": dose.scheduled_for,
                "actioned_at": dose.actioned_at,
            })
    expected = totals["taken"] + totals["skipped"] + totals["missed"]
    for item in by_product.values():
        item["expected"] = item["taken"] + item["skipped"] + item["missed"]
        item["taken_rate"] = round((item["taken"] / item["expected"]) * 100) if item["expected"] else None
        item["history"].sort(key=lambda entry: entry["scheduled_for"])
    return {
        "days": days,
        "start_date": local_start.isoformat(),
        "end_date": local_today.isoformat(),
        "taken": totals["taken"],
        "skipped": totals["skipped"],
        "missed": totals["missed"],
        "pending": totals["pending"],
        "expected": expected,
        "taken_rate": round((totals["taken"] / expected) * 100) if expected else None,
        "medicines": sorted(by_product.values(), key=lambda item: (-item["expected"], item["medicine_name"])),
        "exceptions": exceptions[:10],
    }


@app.get("/api/v1/reports/compliance")
def compliance_report(days: int = 7, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return build_compliance_report(user, session, days)


@app.get("/api/v1/reports/compliance.pdf")
def compliance_report_pdf(days: int = 7, user: User = Depends(current_user), session: Session = Depends(get_session)):
    report = build_compliance_report(user, session, days)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="DoseKeep medication compliance report",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("DoseKeep", styles["Title"]),
        Paragraph("Medication compliance report", styles["Heading1"]),
        Paragraph(f"{report['start_date']} to {report['end_date']} · regular doses only · generated {datetime.now().strftime('%d %b %Y %H:%M')}", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    rate = "No completed doses" if report["taken_rate"] is None else f"{report['taken_rate']}% taken"
    summary = [
        ["Taken rate", "Taken", "Skipped", "Not recorded"],
        [rate, str(report["taken"]), str(report["skipped"]), str(report["missed"])],
    ]
    table = Table(summary, colWidths=[42 * mm] * 4)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6373D2")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#DCE1ED")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F7F8FC")),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 7 * mm)])
    story.append(Paragraph("Medication details", styles["Heading2"]))

    def local_time(value: datetime | None) -> str:
        if not value:
            return "—"
        return value.replace(tzinfo=timezone.utc).astimezone(zone).strftime("%d %b %Y %H:%M")

    outcome_labels = {
        "taken": "Taken",
        "skipped": "Skipped",
        "missed": "Not recorded",
        "pending": "Pending",
    }
    for medicine in report["medicines"]:
        story.append(Paragraph(html.escape(medicine["medicine_name"]), styles["Heading3"]))
        medicine_rate = "No completed doses" if medicine["taken_rate"] is None else f"{medicine['taken_rate']}% taken"
        medicine_summary = [
            ["Taken rate", "Taken", "Skipped", "Not recorded"],
            [medicine_rate, str(medicine["taken"]), str(medicine["skipped"]), str(medicine["missed"])],
        ]
        medicine_summary_table = Table(medicine_summary, colWidths=[42 * mm] * 4)
        medicine_summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7462BB")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#DCE1ED")),
            ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F7F8FC")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        history_rows = [["Scheduled dose", "Quantity", "Outcome", "Recorded"]]
        for entry in medicine["history"]:
            scheduled = entry["scheduled_for"].replace(tzinfo=timezone.utc).astimezone(zone)
            history_rows.append([
                f"{scheduled.strftime('%d %b %Y')} · {entry['administration_time'].title()} ({scheduled.strftime('%H:%M')})",
                f"{entry['quantity']:g}",
                outcome_labels[entry["outcome"]],
                local_time(entry["actioned_at"]),
            ])
        history_table = Table(history_rows, colWidths=[68 * mm, 22 * mm, 34 * mm, 44 * mm], repeatRows=1)
        history_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EDF8")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17233D")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#DCE1ED")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.extend([medicine_summary_table, Spacer(1, 3 * mm), history_table, Spacer(1, 6 * mm)])
    story.append(Paragraph("This report records actions documented in DoseKeep. It is not prescribing or clinical advice.", styles["Italic"]))
    document.build(story)
    filename = f"dosekeep-compliance-{days}-days.pdf"
    return Response(content=buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def apply_scheduled_dose_action(dose: ScheduledDose, user: User, payload: ScheduledDoseAction, session: Session) -> None:
    if dose.status in {"taken", "skipped"}:
        raise HTTPException(status_code=409, detail="This dose has already been actioned")
    now = datetime.utcnow()
    if now < dose.due_at:
        early_window = dose.due_at - timedelta(hours=1)
        if payload.action != "taken":
            raise HTTPException(status_code=409, detail="A dose can only be skipped or snoozed once it is due")
        if now < early_window:
            raise HTTPException(
                status_code=409,
                detail="This dose can be recorded from one hour before its scheduled time",
            )
    if payload.action == "snooze":
        dose.status = "snoozed"
        dose.due_at = now + timedelta(minutes=payload.snooze_minutes)
        dose.notes = payload.notes or f"Snoozed for {payload.snooze_minutes} minutes"
        dose.notified_at = None
    elif payload.action == "skipped":
        dose.status = "skipped"
        dose.actioned_at = now
        dose.notes = payload.notes
    else:
        record_taken_dose(dose, user, session, now, payload.notes)


def notification_link(token: str, session: Session) -> NotificationActionLink:
    record = session.query(NotificationActionLink).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not record or record.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=410, detail="This reminder action link has expired")
    return record


def reminder_doses(record: NotificationActionLink, session: Session) -> list[ScheduledDose]:
    try:
        dose_ids = [int(value) for value in json.loads(record.dose_ids)]
    except (TypeError, ValueError, json.JSONDecodeError):
        dose_ids = []
    return (
        session.query(ScheduledDose)
        .filter(ScheduledDose.user_id == record.user_id, ScheduledDose.id.in_(dose_ids))
        .order_by(ScheduledDose.due_at)
        .all()
    )


@app.get("/dose-actions/{token}", response_class=HTMLResponse)
def reminder_action_page(token: str, session: Session = Depends(get_session)):
    record = notification_link(token, session)
    user = session.get(User, record.user_id)
    names = medication_display_names(user, session)
    zone = user_zone(user)
    cards = []
    for dose in reminder_doses(record, session):
        if dose.status not in {"due", "snoozed", "prepared"}:
            continue
        due = dose.due_at.replace(tzinfo=timezone.utc).astimezone(zone).strftime("%H:%M")
        medicine = html.escape(names.get(dose.product_id) or (session.get(Product, dose.product_id).name if session.get(Product, dose.product_id) else "Medicine"))
        prepared_label = "prepared in pill box · " if dose.prepared_at else ""
        cards.append(
            f'<section><h2>{medicine}</h2><p>{html.escape(dose.administration_time.title())} · {dose.quantity:g} item{'s' if dose.quantity != 1 else ''} · {prepared_label}due {due}</p>'
            f'<div class="actions" data-dose-id="{dose.id}"><button data-action="skipped" class="skip">Skip</button>'
            '<button data-action="taken" class="taken">Taken</button><button data-action="snooze">Snooze 15 min</button></div></section>'
        )
    expiry = record.expires_at.replace(tzinfo=timezone.utc).astimezone(zone).strftime("%H:%M")
    prepared_pending = [dose for dose in reminder_doses(record, session) if dose.prepared_at and dose.status in {"due", "snoozed", "prepared"}]
    batch_control = ""
    if len(prepared_pending) > 1 and len({dose.administration_time for dose in prepared_pending}) == 1:
        dose_ids = html.escape(json.dumps([dose.id for dose in prepared_pending]), quote=True)
        batch_control = f'<button id="take-prepared" data-dose-ids="{dose_ids}">Record all prepared doses as taken</button>'
    body = "".join(cards) or "<p>All doses on this reminder have already been recorded.</p>"
    return HTMLResponse(
        f"""<!doctype html><html lang=\"en\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>DoseKeep reminder</title>
<style>body{{font-family:system-ui,sans-serif;background:#f7f8f4;color:#173222;margin:0;padding:1.25rem;max-width:38rem}}section{{background:#fff;padding:1.1rem;margin:1rem 0;border-radius:1rem;box-shadow:0 1px 3px #0002}}h1,h2{{margin:.1rem 0 .5rem}}p{{color:#475569}}.actions{{display:grid;gap:.55rem}}button{{border:0;border-radius:.55rem;padding:.85rem;font:inherit;font-weight:700;background:#475569;color:#fff}}button.taken{{background:#166534;padding:1rem;font-size:1.05rem}}button.skip{{background:#fff;color:#334155;border:1px solid #94a3b8}}#result{{font-weight:700}}</style>
<h1>DoseKeep</h1><p>Record this reminder without signing in. This secure link expires at {expiry}.</p>{batch_control}{body}<p id=\"result\"></p>
<script>const bulk=document.querySelector('#take-prepared'); if(bulk) bulk.addEventListener('click',async()=>{{ bulk.disabled=true; const result=document.querySelector('#result'); try {{ const response=await fetch(location.pathname,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action:'take_prepared',dose_ids:JSON.parse(bulk.dataset.doseIds)}})}}); const data=await response.json(); if(!response.ok) throw new Error(data.detail||'Could not record prepared doses'); document.querySelectorAll('section').forEach(item=>item.remove()); bulk.remove(); result.textContent=`${{data.taken_count}} prepared doses recorded as taken.`; }} catch(error) {{ result.textContent=error.message; bulk.disabled=false; }} }})); document.querySelectorAll('.actions button').forEach(button => button.addEventListener('click', async () => {{
  const actions = button.parentElement; actions.querySelectorAll('button').forEach(item => item.disabled = true);
  const result = document.querySelector('#result');
  try {{ const response = await fetch(location.pathname, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dose_id:Number(actions.dataset.doseId),action:button.dataset.action}})}}); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Could not record dose'); actions.parentElement.remove(); result.textContent = `${{data.medicine_name}} recorded as ${{data.status}}.`; }}
  catch (error) {{ result.textContent = error.message; actions.querySelectorAll('button').forEach(item => item.disabled = false); }}
}}));</script></html>"""
    )


@app.post("/dose-actions/{token}")
async def action_reminder_dose(token: str, request: Request, session: Session = Depends(get_session)):
    record = notification_link(token, session)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Choose a valid reminder action")
    if body.get("action") == "take_prepared":
        try:
            requested = {int(value) for value in body.get("dose_ids", [])}
        except (TypeError, ValueError):
            requested = set()
        doses = [item for item in reminder_doses(record, session) if item.id in requested]
        if not requested or len(doses) != len(requested) or not all(item.prepared_at for item in doses):
            raise HTTPException(status_code=422, detail="Choose only prepared doses from this reminder")
        user = session.get(User, record.user_id)
        take_prepared_dose_group(doses, user, session)
        return {"taken_count": len(doses), "status": "taken"}
    try:
        payload = ScheduledDoseAction.model_validate(body)
        dose_id = int(body.get("dose_id"))
    except Exception:
        raise HTTPException(status_code=422, detail="Choose a valid reminder action")
    dose = next((item for item in reminder_doses(record, session) if item.id == dose_id), None)
    if not dose:
        raise HTTPException(status_code=404, detail="Dose is not part of this reminder")
    user = session.get(User, record.user_id)
    apply_scheduled_dose_action(dose, user, payload, session)
    session.commit()
    session.refresh(dose)
    return dose_read(dose, session, medication_display_names(user, session))


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
    apply_scheduled_dose_action(dose, user, payload, session)
    session.commit()
    session.refresh(dose)
    return dose_read(dose, session, medication_display_names(user, session))


@app.post("/api/v1/doses/{dose_id}/prepared-resolution", response_model=ScheduledDoseRead)
def resolve_skipped_prepared_dose(
    dose_id: int,
    payload: PreparedDoseResolution,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Reconcile the tablet left in a pill box after a skipped prepared dose."""
    dose = session.query(ScheduledDose).filter_by(id=dose_id, user_id=user.id).first()
    if not dose:
        raise HTTPException(status_code=404, detail="Scheduled dose not found")
    if dose.status != "skipped" or not dose.prepared_at:
        raise HTTPException(status_code=409, detail="Only a skipped prepared dose can be reconciled this way")
    allocations = prepared_allocations(dose, session)
    if not allocations:
        raise HTTPException(status_code=409, detail="This prepared dose has already been reconciled")
    if payload.action == "dispose" and not (payload.reason or "").strip():
        raise HTTPException(status_code=422, detail="Give a reason before removing a prepared tablet")
    now = datetime.utcnow()
    for allocation in allocations:
        pack = session.get(Pack, allocation.pack_id)
        if not pack or pack.quantity_in_dosette + 1e-9 < allocation.quantity:
            raise HTTPException(status_code=409, detail="The pill-box count is too low; correct the physical count first")
        pack.quantity_in_dosette = round(pack.quantity_in_dosette - allocation.quantity, 6)
        if payload.action == "return_to_pack":
            pack.quantity_remaining = round(pack.quantity_remaining + allocation.quantity, 6)
            event_type, notes = "returned_to_pack", f"Returned unused prepared dose to pack ({dose.administration_time})"
            allocation.status = "returned"
        else:
            event_type, notes = "disposed", f"Unused prepared dose removed: {payload.reason.strip()}"
            allocation.status = "disposed"
        allocation.resolved_at = now
        session.add(SupplyEvent(pack_id=pack.id, event_type=event_type, quantity=allocation.quantity, notes=notes))
    session.commit()
    session.refresh(dose)
    return dose_read(dose, session, medication_display_names(user, session))


@app.get("/api/v1/devices", response_model=list[DeviceTokenRead])
def list_device_tokens(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return session.query(DeviceToken).filter_by(user_id=user.id).order_by(DeviceToken.created_at.desc()).all()


@app.post("/api/v1/devices", response_model=DeviceTokenCreated, status_code=201)
def create_device_token(payload: DeviceTokenCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    owned = session.query(Pack).filter(accessible_pack_filter(user, session), Pack.product_id == payload.product_id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Medicine not found")
    token = f"dosekeep_{secrets.token_urlsafe(32)}"
    record = DeviceToken(
        user_id=user.id,
        product_id=payload.product_id,
        administration_time=payload.administration_time,
        label=payload.label.strip(),
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return {**DeviceTokenRead.model_validate(record).model_dump(), "token": token}


@app.delete("/api/v1/devices/{device_id}")
def revoke_device_token(device_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    record = session.query(DeviceToken).filter_by(id=device_id, user_id=user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Device token not found")
    record.revoked_at = datetime.utcnow()
    session.commit()
    return {"ok": True}


@app.post("/api/v1/device/taken")
def record_device_taken(authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    """Inbound physical-button endpoint, scoped by a revocable bearer token."""
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not token.startswith("dosekeep_"):
        raise HTTPException(status_code=401, detail="A device bearer token is required")
    record = session.query(DeviceToken).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not record or record.revoked_at:
        raise HTTPException(status_code=401, detail="Device token is invalid or revoked")
    user = session.get(User, record.user_id)
    zone = user_zone(user)
    now = datetime.utcnow()
    local_today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    start = datetime.combine(local_today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    dose = (
        session.query(ScheduledDose)
        .filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.product_id == record.product_id,
            ScheduledDose.administration_time == record.administration_time,
            ScheduledDose.scheduled_for >= start,
            ScheduledDose.scheduled_for < end,
            ScheduledDose.status.in_(("due", "snoozed")),
        )
        .order_by(ScheduledDose.due_at)
        .first()
    )
    if not dose:
        already = session.query(ScheduledDose).filter(
            ScheduledDose.user_id == user.id,
            ScheduledDose.product_id == record.product_id,
            ScheduledDose.administration_time == record.administration_time,
            ScheduledDose.scheduled_for >= start,
            ScheduledDose.scheduled_for < end,
            ScheduledDose.status == "taken",
        ).first()
        if already:
            return {"ok": True, "already_recorded": True, "dose_id": already.id}
        raise HTTPException(status_code=409, detail="No due dose is available for this device")
    if now < dose.due_at - timedelta(hours=1):
        raise HTTPException(status_code=409, detail="This dose is not yet within its one-hour early recording window")
    record_taken_dose(dose, user, session, now, f"Recorded by device: {record.label}")
    record.last_used_at = now
    session.commit()
    return {"ok": True, "already_recorded": False, "dose_id": dose.id}


@app.post("/api/v1/products/{product_id}/prn", response_model=ScheduledDoseRead, status_code=201)
def record_prn_dose(product_id: int, payload: PRNDoseCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    product_ids = related_product_ids(user, product_id, session)
    schedules = session.query(MedicationSchedule).filter(
        MedicationSchedule.user_id == user.id,
        MedicationSchedule.product_id.in_(product_ids),
    ).all()
    if not schedules or not any(schedule.as_required for schedule in schedules):
        raise HTTPException(status_code=409, detail="This medicine does not have an As required (PRN) plan")
    now = datetime.utcnow()
    consume_stock(user, product_id, payload.quantity, session, "PRN dose")
    record = ScheduledDose(
        user_id=user.id,
        product_id=product_id,
        administration_time="prn",
        scheduled_for=now,
        due_at=now,
        status="taken",
        actioned_at=now,
        quantity=payload.quantity,
        notes="Recorded PRN dose",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return dose_read(record, session, medication_display_names(user, session))


@app.post("/api/v1/products", response_model=ProductRead, status_code=201)
def create_product(product: ProductCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    record = Product(**product.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.get("/api/v1/packs", response_model=list[PackRead])
def list_packs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return session.query(Pack).filter(accessible_pack_filter(user, session)).order_by(Pack.expiry_date.is_(None), Pack.expiry_date).all()


@app.get("/api/v1/packs/{pack_id}/events", response_model=list[SupplyEventRead])
def list_pack_events(pack_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not session.query(Pack).filter(Pack.id == pack_id, accessible_pack_filter(user, session)).first():
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

    consolidate_medikeep_products(user, session)

    existing_pack = session.query(Pack).filter(Pack.user_id == user.id, Pack.gtin == gtin, Pack.serial_number == decoded.get("serial_number")).first()
    if existing_pack:
        return {"product": existing_pack.product, "pack": existing_pack, "created": False}

    product = None
    if scan.existing_product_id is not None:
        product = session.get(Product, scan.existing_product_id)
        owned = product and (
            session.query(Pack).filter_by(user_id=user.id, product_id=product.id).first()
            or session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product.id).first()
        )
        if not owned:
            raise HTTPException(status_code=404, detail="Existing DoseKeep medicine was not found")
        existing_link = session.query(MediKeepLink).filter_by(user_id=user.id, product_id=product.id).first()
        if existing_link and scan.medikeep_medication_id is not None and existing_link.medikeep_medication_id != scan.medikeep_medication_id:
            raise HTTPException(status_code=422, detail="That existing medicine is linked to a different MediKeep medicine")
    elif scan.medikeep_medication_id is not None:
        existing_link = session.query(MediKeepLink).filter_by(
            user_id=user.id,
            medikeep_medication_id=scan.medikeep_medication_id,
        ).first()
        product = existing_link.product if existing_link else None
    if not product:
        product = session.query(Product).filter(Product.barcode == gtin).first()
    if not product:
        product = Product(
            name=catalogue_product.name,
            form=catalogue_product.form,
            category=scan.category,
            barcode=gtin,
            catalogue_source=catalogue_product.source,
            leaflet_url=catalogue_product.leaflet_url,
        )
        session.add(product)
        session.flush()
    elif not product.leaflet_url and catalogue_product.leaflet_url:
        product.leaflet_url = catalogue_product.leaflet_url
    save_default_common_name(user, product.id, catalogue_product.common_name, session)
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
    pack = session.query(Pack).filter(Pack.id == pack_id, accessible_pack_filter(user, session)).first()
    if not pack or not can_change_pack(user, pack, session):
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
    pack = session.query(Pack).filter(Pack.id == pack_id, accessible_pack_filter(user, session)).first()
    if not pack or not can_change_pack(user, pack, session):
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
    elif event.event_type in {"disposed", "removed_from_stock"}:
        if pack.quantity_remaining < event.quantity:
            raise HTTPException(status_code=409, detail="Insufficient pack stock")
        if event.event_type == "removed_from_stock" and not (event.notes or "").strip():
            raise HTTPException(status_code=422, detail="Give a reason for removing stock")
        pack.quantity_remaining -= event.quantity
    record = SupplyEvent(pack_id=pack_id, **event.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
