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
    is_admin: bool = False

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


class NotificationSettings(BaseModel):
    """Per-account ntfy destination for dose reminders."""

    enabled: bool = False
    server_url: str = Field(default="https://ntfy.sh", min_length=8, max_length=500)
    topic: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    def validate_destination(self) -> None:
        if self.enabled and not self.topic:
            raise ValueError("Enter an ntfy topic before enabling reminders")
        if not self.server_url.startswith("https://"):
            raise ValueError("Use an HTTPS ntfy server URL")


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
    quantity_initial: float = Field(gt=0)
    quantity_remaining: float = Field(ge=0)
    obtained_on: date | None = None
    household_id: int | None = None


class PackRead(PackCreate):
    id: int
    status: str
    quantity_in_dosette: float
    created_at: datetime

    model_config = {"from_attributes": True}


class SupplyEventCreate(BaseModel):
    event_type: str = Field(pattern="^(dosette_fill|taken|taken_from_pack|skipped|disposed|removed_from_stock|correction)$")
    quantity: float = Field(gt=0)
    notes: str | None = None


class SupplyEventRead(SupplyEventCreate):
    id: int
    pack_id: int
    occurred_at: datetime
    actor_name: str | None = None

    model_config = {"from_attributes": True}


class CabinetGuestLinkCreate(BaseModel):
    label: str = Field(default="Cabinet QR code", min_length=1, max_length=120)
    require_name: bool = True


class HouseholdInviteCreate(BaseModel):
    add_to_household: bool = True
    role: str = Field(default="viewer", pattern="^(viewer|contributor|admin)$")


class StockCorrection(BaseModel):
    quantity_remaining: float = Field(ge=0)
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
    common_name: str | None = None
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
    common_name: str | None = None
    dosage: str | None = None
    route: str | None = None
    frequency: str | None = None
    quantity_remaining: float = 0
    quantity_in_dosette: float = 0
    active_pack_count: int = 0
    linked_to_medikeep: bool = False
    medikeep_status: str | None = None
    regular_times: list[str] = []
    dose_quantities: dict[str, float] = {}
    as_required: bool = False
    prn_notes: str | None = None
    daily_dose_count: float = 0
    estimated_run_out_date: date | None = None
    stock_status: str = "unknown"


class MedicationScheduleUpdate(BaseModel):
    regular_times: list[str] = []
    dose_quantities: dict[str, float] = {}
    as_required: bool = False
    prn_notes: str | None = Field(default=None, max_length=500)

    def validate_schedule(self) -> None:
        allowed = {"morning", "midday", "evening", "bedtime"}
        if any(value not in allowed for value in self.regular_times):
            raise ValueError("Administration times must be morning, midday, evening or bedtime")
        if any(slot not in allowed for slot in self.dose_quantities):
            raise ValueError("Dose quantities must use a valid administration time")
        if any(quantity <= 0 or quantity > 100 for quantity in self.dose_quantities.values()):
            raise ValueError("Each dose quantity must be greater than 0 and no more than 100 items")


class CommonNameUpdate(BaseModel):
    common_name: str = Field(min_length=1, max_length=200)


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
    quantity: float = 1
    actioned_at: datetime | None = None
    notes: str | None = None
    stock_available: float = 0


class ScheduledDoseAction(BaseModel):
    action: str = Field(pattern="^(taken|skipped|snooze)$")
    snooze_minutes: int = Field(default=15, ge=5, le=240)
    notes: str | None = Field(default=None, max_length=500)


class PRNDoseCreate(BaseModel):
    quantity: float = Field(default=1, gt=0, le=100)


class ScannedPackCreate(BaseModel):
    raw: str
    quantity_initial: int = Field(gt=0)
    obtained_on: date | None = None
    category: str = Field(default="medicine", pattern="^(medicine|otc|supplement|other)$")
    medikeep_medication_id: int | None = None
    # A scanned barcode may be another pack of a medicine already tracked in
    # DoseKeep, rather than a new medicine in its own right.
    existing_product_id: int | None = None
    household_id: int | None = None


class ScannedPackRead(BaseModel):
    product: ProductRead
    pack: PackRead
    created: bool


class HouseholdCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class HouseholdMemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="viewer", pattern="^(admin|contributor|viewer)$")


class HouseholdRead(BaseModel):
    id: int
    name: str
    role: str
    member_count: int


class PackHouseholdUpdate(BaseModel):
    household_id: int | None = None


class DeviceTokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    product_id: int
    administration_time: str = Field(pattern="^(morning|midday|evening|bedtime)$")


class DeviceTokenRead(BaseModel):
    id: int
    label: str
    product_id: int
    administration_time: str
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeviceTokenCreated(DeviceTokenRead):
    token: str
