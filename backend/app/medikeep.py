"""Optional MediKeep connector.

DoseKeep normally reads a user's MediKeep list. A user may explicitly choose
to create a medicine from a confirmed pack scan; that is the only write path
and it always leaves clinical directions for review in MediKeep.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass


class MediKeepUnavailable(RuntimeError):
    """Raised when the optional connector has not been configured or fails."""


@dataclass(frozen=True)
class MediKeepMedication:
    id: int
    name: str
    dosage: str | None
    route: str | None
    frequency: str | None
    status: str


def _token(config: dict) -> str:
    configured = (config.get("token") or "").strip()
    if configured:
        return configured
    username = (config.get("username") or "").strip()
    password = config.get("password") or ""
    if not username or not password:
        raise MediKeepUnavailable("Set a MediKeep bearer token or username and password")
    payload = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        f"{config['base_url'].rstrip('/')}/auth/login",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())["access_token"]
    except Exception as error:  # Network/auth details should not reach the UI.
        raise MediKeepUnavailable("Could not authenticate to MediKeep") from error


def all_medications(config: dict) -> list[MediKeepMedication]:
    base = (config.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise MediKeepUnavailable("MediKeep connection is not configured")
    try:
        patient_id = int(config.get("patient_id", 1))
    except (TypeError, ValueError) as error:
        raise MediKeepUnavailable("MediKeep patient ID must be a number") from error
    token = _token(config)
    request = urllib.request.Request(
        f"{base}/medications/?patient_id={patient_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            records = json.loads(response.read().decode())
    except Exception as error:
        raise MediKeepUnavailable("Could not read active medications from MediKeep") from error
    return [
        MediKeepMedication(
            id=int(record["id"]),
            name=record.get("medication_name") or "Unnamed medication",
            dosage=record.get("dosage") or None,
            route=record.get("route") or None,
            frequency=record.get("frequency") or None,
            status=record.get("status") or "unknown",
        )
        for record in records
    ]


def active_medications(config: dict) -> list[MediKeepMedication]:
    return [medication for medication in all_medications(config) if medication.status == "active"]


def normalize_name(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    value = value.replace("atorvastatine", "atorvastatin")
    return {token for token in re.findall(r"[a-z0-9]+", value) if len(token) > 2 or token.isdigit()}


def suggested_medications(config: dict, product_name: str) -> list[MediKeepMedication]:
    product_tokens = normalize_name(product_name)
    suggestions: list[tuple[int, MediKeepMedication]] = []
    # A recently replaced medicine may no longer be active in MediKeep but can
    # still be a real pack in the cupboard, so allow it to be linked on scan.
    for medication in all_medications(config):
        medication_tokens = normalize_name(f"{medication.name} {medication.dosage or ''}")
        overlap = len(product_tokens & medication_tokens)
        if overlap:
            suggestions.append((overlap, medication))
    return [medication for _, medication in sorted(suggestions, key=lambda entry: (-entry[0], entry[1].name))]


def create_medication(config: dict, *, name: str, dosage: str | None, category: str) -> MediKeepMedication:
    """Create a MediKeep medicine only after an explicit DoseKeep confirmation."""
    base = (config.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise MediKeepUnavailable("MediKeep connection is not configured")
    try:
        patient_id = int(config.get("patient_id", 1))
    except (TypeError, ValueError) as error:
        raise MediKeepUnavailable("MediKeep patient ID must be a number") from error
    medication_type = {"medicine": "prescription", "otc": "otc", "supplement": "supplement", "other": "otc"}.get(category, "otc")
    payload = {
        "patient_id": patient_id,
        "medication_name": name,
        "medication_type": medication_type,
        "status": "active",
        "notes": "Added from a confirmed DoseKeep pack scan. Review directions and status.",
    }
    if dosage:
        payload["dosage"] = dosage
    request = urllib.request.Request(
        f"{base}/medications/",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_token(config)}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            record = json.loads(response.read().decode())
    except Exception as error:
        raise MediKeepUnavailable("Could not create the medicine in MediKeep") from error
    return MediKeepMedication(
        id=int(record["id"]),
        name=record.get("medication_name") or name,
        dosage=record.get("dosage") or dosage,
        route=record.get("route") or None,
        frequency=record.get("frequency") or None,
        status=record.get("status") or "active",
    )
