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


class AdministrationTimeSettings(BaseModel):
    morning: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    midday: str = Field(default="12:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    evening: str = Field(default="19:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    bedtime: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


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


class MedicineOverviewRead(BaseModel):
    """One user-facing medicine card, independent of individual packs."""

    key: str
    product_id: int | None = None
    medikeep_medication_id: int | None = None
    name: str
    dosage: str | None = None
    route: str | None = None
    frequency: str | None = None
    quantity_remaining: int = 0
    quantity_in_dosette: int = 0
    active_pack_count: int = 0
    linked_to_medikeep: bool = False
    medikeep_status: str | None = None
    regular_times: list[str] = []
    as_required: bool = False
    prn_notes: str | None = None


class MedicationScheduleUpdate(BaseModel):
    regular_times: list[str] = []
    as_required: bool = False
    prn_notes: str | None = Field(default=None, max_length=500)

    def validate_schedule(self) -> None:
        allowed = {"morning", "midday", "evening", "bedtime"}
        if any(value not in allowed for value in self.regular_times):
            raise ValueError("Administration times must be morning, midday, evening or bedtime")


class ScheduledDoseRead(BaseModel):
    id: int
    product_id: int
    medicine_name: str
    dosage: str | None = None
    route: str | None = None
    administration_time: str
    scheduled_for: datetime
    due_at: datetime
    status: str
    actioned_at: datetime | None = None
    notes: str | None = None
    stock_available: int = 0


class ScheduledDoseAction(BaseModel):
    action: str = Field(pattern="^(taken|skipped|snooze)$")
    snooze_minutes: int = Field(default=15, ge=5, le=240)
    notes: str | None = Field(default=None, max_length=500)


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
