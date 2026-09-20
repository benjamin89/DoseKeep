from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .database import Base, engine, get_session
from .gs1 import parse_medicine_code
from .models import Pack, Product, SupplyEvent
from .schemas import DecodedCode, PackCreate, PackRead, ProductCreate, ProductRead, SupplyEventCreate, SupplyEventRead


Base.metadata.create_all(bind=engine)

app = FastAPI(title="DoseKeep", version="0.1.0")
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


@app.post("/api/v1/packs", response_model=PackRead, status_code=201)
def create_pack(pack: PackCreate, session: Session = Depends(get_session)):
    if not session.get(Product, pack.product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    record = Pack(**pack.model_dump(exclude_none=True), obtained_on=pack.obtained_on or date.today())
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@app.post("/api/v1/packs/{pack_id}/events", response_model=SupplyEventRead, status_code=201)
def add_supply_event(pack_id: int, event: SupplyEventCreate, session: Session = Depends(get_session)):
    pack = session.get(Pack, pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Pack not found")
    if event.event_type in {"dosette_fill", "taken", "disposed"}:
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

