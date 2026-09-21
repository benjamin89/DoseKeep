"""Optional, read-only MediKeep connector.

DoseKeep never writes medication records back to MediKeep. It only reads the
current user's active list, then stores an optional MediKeep medication ID on
its own local product record.
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


def active_medications(config: dict) -> list[MediKeepMedication]:
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
        if record.get("status") == "active"
    ]


def normalize_name(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    value = value.replace("atorvastatine", "atorvastatin")
    return {token for token in re.findall(r"[a-z0-9]+", value) if len(token) > 2}


def suggested_medications(config: dict, product_name: str) -> list[MediKeepMedication]:
    product_tokens = normalize_name(product_name)
    suggestions: list[tuple[int, MediKeepMedication]] = []
    for medication in active_medications(config):
        medication_tokens = normalize_name(f"{medication.name} {medication.dosage or ''}")
        overlap = len(product_tokens & medication_tokens)
        if overlap:
            suggestions.append((overlap, medication))
    return [medication for _, medication in sorted(suggestions, key=lambda entry: (-entry[0], entry[1].name))]
