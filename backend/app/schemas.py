from datetime import date, datetime

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    strength: str | None = None
    form: str | None = None
    category: str = "medicine"
    barcode: str | None = None
    catalogue_source: str | None = None
    medikeep_medication_id: int | None = None
    notes: str | None = None


class ProductRead(ProductCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class PackCreate(BaseModel):
    product_id: int
    gtin: str | None = None
    serial_number: str | None = None
    batch_number: str | None = None
    expiry_date: date | None = None
    quantity_initial: int = Field(gt=0)
    quantity_remaining: int = Field(ge=0)
    obtained_on: date | None = None


class PackRead(PackCreate):
    id: int
    status: str
    quantity_in_dosette: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SupplyEventCreate(BaseModel):
    event_type: str = Field(pattern="^(dosette_fill|taken|skipped|disposed|correction)$")
    quantity: int = Field(gt=0)
    notes: str | None = None


class SupplyEventRead(SupplyEventCreate):
    id: int
    pack_id: int
    occurred_at: datetime

    model_config = {"from_attributes": True}


class DecodedCode(BaseModel):
    raw: str
    gtin: str | None = None
    serial_number: str | None = None
    expiry_date: date | None = None
    batch_number: str | None = None


class CatalogueProductRead(BaseModel):
    gtin: str
    name: str
    form: str | None = None
    route: str | None = None
    holder: str | None = None
    presentation: str | None = None
    quantity_hint: int | None = None
    source: str


class ScannedPackCreate(BaseModel):
    raw: str
    quantity_initial: int = Field(gt=0)
    obtained_on: date | None = None


class ScannedPackRead(BaseModel):
    product: ProductRead
    pack: PackRead
    created: bool
