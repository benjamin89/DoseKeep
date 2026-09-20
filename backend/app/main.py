from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .catalogue import lookup_french_gtin
from .database import Base, engine, ensure_schema, get_session
from .gs1 import parse_medicine_code
from .models import Pack, Product, SupplyEvent
from .schemas import (
    CatalogueProductRead,
    DecodedCode,
    PackCreate,
    PackRead,
    ProductCreate,
    ProductRead,
    ScannedPackCreate,
    ScannedPackRead,
    StockCorrection,
    SupplyEventCreate,
    SupplyEventRead,
)


Base.metadata.create_all(bind=engine)
ensure_schema()

app = FastAPI(title="DoseKeep", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dosekeep"}


@app.post("/api/v1/scan/parse", response_model=DecodedCode)
def parse_scan(raw: str):
    try:
        return parse_medicine_code(raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/catalogue/fr/{gtin}", response_model=CatalogueProductRead)
def lookup_french_catalogue(gtin: str):
    product = lookup_french_gtin(gtin)
    if not product:
        raise HTTPException(status_code=404, detail="No active French catalogue match")
    return {"gtin": gtin, **product.__dict__}


@app.get("/api/v1/products", response_model=list[ProductRead])
def list_products(session: Session = Depends(get_session)):
    return session.query(Product).order_by(Product.name).all()


@app.post("/api/v1/products", response_model=ProductRead, status_code=201)
def create_product(product: ProductCreate, session: Session = Depends(get_session)):
    record = Product(**product.model_dump())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.get("/api/v1/packs", response_model=list[PackRead])
def list_packs(session: Session = Depends(get_session)):
    return session.query(Pack).order_by(Pack.expiry_date.is_(None), Pack.expiry_date).all()


@app.get("/api/v1/packs/{pack_id}/events", response_model=list[SupplyEventRead])
def list_pack_events(pack_id: int, session: Session = Depends(get_session)):
    if not session.get(Pack, pack_id):
        raise HTTPException(status_code=404, detail="Pack not found")
    return (
        session.query(SupplyEvent)
        .filter(SupplyEvent.pack_id == pack_id)
        .order_by(SupplyEvent.occurred_at.desc())
        .limit(10)
        .all()
    )


@app.post("/api/v1/packs", response_model=PackRead, status_code=201)
def create_pack(pack: PackCreate, session: Session = Depends(get_session)):
    if not session.get(Product, pack.product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    record = Pack(**pack.model_dump(exclude_none=True), obtained_on=pack.obtained_on or date.today())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.post("/api/v1/packs/from-scan", response_model=ScannedPackRead, status_code=201)
def create_pack_from_scan(scan: ScannedPackCreate, session: Session = Depends(get_session)):
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

    existing_pack = session.query(Pack).filter(Pack.gtin == gtin, Pack.serial_number == decoded.get("serial_number")).first()
    if existing_pack:
        return {"product": existing_pack.product, "pack": existing_pack, "created": False}

    product = session.query(Product).filter(Product.barcode == gtin).first()
    if not product:
        product = Product(
            name=catalogue_product.name,
            form=catalogue_product.form,
            barcode=gtin,
            catalogue_source=catalogue_product.source,
        )
        session.add(product)
        session.flush()
    pack = Pack(
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
def correct_pack_stock(pack_id: int, correction: StockCorrection, session: Session = Depends(get_session)):
    """Record a physical count without inventing historical dose events."""
    pack = session.get(Pack, pack_id)
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
def add_supply_event(pack_id: int, event: SupplyEventCreate, session: Session = Depends(get_session)):
    pack = session.get(Pack, pack_id)
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
