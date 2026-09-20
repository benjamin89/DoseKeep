"""French public-medicines catalogue resolver.

The official BDPM files are downloaded to the local DoseKeep data volume and
refreshed weekly. A lookup sends only a GTIN/CIP identifier, never pack serial
or patient information.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen


BDPM_BASE = "https://base-donnees-publique.medicaments.gouv.fr/download/file"
FILES = ("CIS_CIP_bdpm.txt", "CIS_bdpm.txt")
REFRESH_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class CatalogueProduct:
    name: str
    form: str | None
    route: str | None
    holder: str | None
    presentation: str | None
    quantity_hint: int | None
    source: str = "France — BDPM"


def _catalogue_dir() -> Path:
    directory = Path("/data/catalogue")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _ensure_file(filename: str) -> Path:
    path = _catalogue_dir() / filename
    stale = not path.exists() or time.time() - path.stat().st_mtime > REFRESH_SECONDS
    if stale:
        with urlopen(f"{BDPM_BASE}/{filename}", timeout=30) as response:
            path.write_bytes(response.read())
    return path


def _fields(line: bytes) -> list[str]:
    # Current BDPM downloads are UTF-8, while historical exports used a
    # legacy single-byte encoding. Prefer UTF-8 and retain a safe fallback.
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        text = line.decode("latin-1", errors="replace")
    return text.rstrip("\r\n").split("\t")


def _quantity_hint(presentation: str | None) -> int | None:
    if not presentation:
        return None
    match = re.search(r"de\s+(\d+)\s+(?:comprim|capsul|g[ée]lule|sachet|ampoule|dose)", presentation, re.I)
    return int(match.group(1)) if match else None


def lookup_french_gtin(gtin: str) -> CatalogueProduct | None:
    """Resolve a French CIP13/GTIN against the official public BDPM files."""
    cip_path = _ensure_file("CIS_CIP_bdpm.txt")
    # A French CIP13 is commonly carried in a GS1 DataMatrix as a GTIN-14
    # with a leading packaging indicator of zero (e.g. 03400930330531).
    # BDPM stores the underlying 13-digit CIP (3400930330531), so try both.
    identifiers = (gtin, gtin[1:]) if len(gtin) == 14 and gtin.startswith("0") else (gtin,)
    cis_id: str | None = None
    presentation: str | None = None
    with cip_path.open("rb") as file:
        for line in file:
            fields = _fields(line)
            if len(fields) > 6 and fields[6] in identifiers:
                cis_id, presentation = fields[0], fields[2]
                break
    if not cis_id:
        return None

    cis_path = _ensure_file("CIS_bdpm.txt")
    with cis_path.open("rb") as file:
        for line in file:
            fields = _fields(line)
            if fields and fields[0] == cis_id:
                return CatalogueProduct(
                    name=fields[1],
                    form=fields[2] or None,
                    route=fields[3] or None,
                    holder=fields[8].strip() if len(fields) > 8 and fields[8].strip() else None,
                    presentation=presentation,
                    quantity_hint=_quantity_hint(presentation),
                )
    return None
