"""Minimal parser for GS1 element strings from medicine Data Matrix scans."""

from datetime import date


FIXED_LENGTH = {"01": 14, "17": 6}
VARIABLE_LENGTH = {"10", "21"}


def parse_gs1(raw: str) -> dict[str, str]:
    """Parse bracketed GS1 text, e.g. (01)...(17)...(10)... .

    Phone scanners commonly render GS1 application identifiers in brackets.
    This deliberately supports the medicine-pack fields needed for the MVP.
    """
    if not raw.startswith("("):
        return _parse_machine_gs1(raw)
    values: dict[str, str] = {}
    index = 0
    while index < len(raw):
        if raw[index] != "(" or index + 3 >= len(raw) or raw[index + 3] != ")":
            raise ValueError("Expected a GS1 application identifier")
        ai = raw[index + 1 : index + 3]
        index += 4
        if ai in FIXED_LENGTH:
            length = FIXED_LENGTH[ai]
            value = raw[index : index + length]
            if len(value) != length:
                raise ValueError(f"Incomplete GS1 field {ai}")
            index += length
        elif ai in VARIABLE_LENGTH:
            next_ai = raw.find("(", index)
            value = raw[index:] if next_ai == -1 else raw[index:next_ai]
            index = len(raw) if next_ai == -1 else next_ai
            if not value:
                raise ValueError(f"Empty GS1 field {ai}")
        else:
            raise ValueError(f"Unsupported GS1 field {ai}")
        values[ai] = value
    return values


def _parse_machine_gs1(raw: str) -> dict[str, str]:
    """Parse scanner output using FNC1/GS separators rather than brackets."""
    raw = raw.removeprefix("]d2")
    values: dict[str, str] = {}
    index = 0
    while index < len(raw):
        ai = raw[index : index + 2]
        if ai not in FIXED_LENGTH and ai not in VARIABLE_LENGTH:
            raise ValueError(f"Unsupported GS1 field {ai}")
        index += 2
        if ai in FIXED_LENGTH:
            length = FIXED_LENGTH[ai]
            value = raw[index : index + length]
            if len(value) != length:
                raise ValueError(f"Incomplete GS1 field {ai}")
            index += length
        else:
            separator = raw.find("\x1d", index)
            if separator == -1:
                value, index = raw[index:], len(raw)
            else:
                value, index = raw[index:separator], separator + 1
            if not value:
                raise ValueError(f"Empty GS1 field {ai}")
        values[ai] = value
    return values


def parse_medicine_code(raw: str) -> dict[str, object]:
    values = parse_gs1(raw)
    parsed: dict[str, object] = {"raw": raw, "gtin": values.get("01"), "serial_number": values.get("21"), "batch_number": values.get("10")}
    expiry = values.get("17")
    if expiry:
        year, month, day = 2000 + int(expiry[:2]), int(expiry[2:4]), int(expiry[4:6])
        parsed["expiry_date"] = date(year, month, day)
    else:
        parsed["expiry_date"] = None
    return parsed
