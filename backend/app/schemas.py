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


class UserRegister(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)


class UserRead(BaseModel):
    id: int
    email: str

    model_config = {"from_attributes": True}


class AuthStatus(BaseModel):
    authenticated: bool
    user: UserRead | None = None
    medikeep_connected: bool = False


class MediKeepConnectionCreate(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    patient_id: int = Field(default=1, gt=0)
    token: str | None = None
    username: str | None = None
    password: str | None = None

    def validate_credentials(self) -> None:
        if not self.token and not (self.username and self.password):
            raise ValueError("Provide a bearer token or a username and password")


class MediKeepConnectionRead(BaseModel):
    configured: bool
    base_url: str | None = None


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
    event_type: str = Field(pattern="^(dosette_fill|taken|taken_from_pack|skipped|disposed|correction)$")
    quantity: int = Field(gt=0)
    notes: str | None = None


class SupplyEventRead(SupplyEventCreate):
    id: int
    pack_id: int
    occurred_at: datetime

    model_config = {"from_attributes": True}


class StockCorrection(BaseModel):
    quantity_remaining: int = Field(ge=0)
    notes: str | None = None


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


class MediKeepMedicationRead(BaseModel):
    id: int
    name: str
    dosage: str | None = None
    route: str | None = None
    frequency: str | None = None
    status: str


class MediKeepImportRead(BaseModel):
    product: ProductRead
    created: bool


class ScannedPackCreate(BaseModel):
    raw: str
    quantity_initial: int = Field(gt=0)
    obtained_on: date | None = None
    category: str = Field(default="medicine", pattern="^(medicine|otc|supplement|other)$")
    medikeep_medication_id: int | None = None


class ScannedPackRead(BaseModel):
    product: ProductRead
    pack: PackRead
    created: bool
