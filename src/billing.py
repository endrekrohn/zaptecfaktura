"""Shared billing logic for the PDF export and the Fiken drafts.

Both paths use the same numbers: kWh rounded to 3 decimals and a price per
kWh in whole øre, so the PDF attachment always matches the Fiken invoice line.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# Fixed namespace so the same installation and month always gets the same
# draft UUID. Fiken stores it on the draft and on the invoice made from it.
DRAFT_NAMESPACE = uuid.UUID("6b3f0a52-4c1e-4f7e-9d3a-2a8f5e0c7b11")

MAX_NOK_PER_KWH = Decimal("100")


class ValidationError(ValueError):
    pass


@dataclass
class InstallationUsage:
    installation_id: str
    name: str
    sessions: list[dict[str, Any]]
    total_kwh: float


def previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def validate_period(year: int, month: int) -> None:
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        raise ValidationError("Ugyldig periode")


def month_range(year: int, month: int) -> tuple[str, str]:
    """Return the Zaptec From/To timestamps for a calendar month (UTC)."""
    validate_period(year, month)
    from_date = f"{year}-{month:02d}-01T00:00:00Z"
    if month == 12:
        to_date = f"{year + 1}-01-01T00:00:00Z"
    else:
        to_date = f"{year}-{month + 1:02d}-01T00:00:00Z"
    return from_date, to_date


def total_kwh(sessions: list[dict[str, Any]]) -> float:
    return round(sum(s.get("Energy", 0) or 0 for s in sessions), 3)


def parse_nok_per_kwh(value: str | float) -> Decimal:
    try:
        price = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise ValidationError("Ugyldig pris per kWh") from None
    if not price.is_finite() or price < 0 or price > MAX_NOK_PER_KWH:
        raise ValidationError("Ugyldig pris per kWh")
    return price


def nok_to_ore(nok: Decimal) -> int:
    return int((nok * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def line_total_ore(kwh: float, unit_price_ore: int) -> int:
    total = Decimal(str(kwh)) * unit_price_ore
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def draft_uuid(installation_id: str, year: int, month: int) -> str:
    return str(uuid.uuid5(DRAFT_NAMESPACE, f"{installation_id}:{year}-{month:02d}"))


def safe_filename(filename: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", filename)
    return safe.strip(" .").replace(" ", "_") or "installasjon"


def pdf_filename(year: int, month: int, installation_name: str) -> str:
    return f"{year}_{month:02d}_grunnlag_{safe_filename(installation_name)}.pdf"
