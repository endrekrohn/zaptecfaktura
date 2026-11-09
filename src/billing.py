"""Shared billing logic for the PDF export and the Fiken drafts.

Both paths use the same numbers: kWh rounded to 3 decimals and a price per
kWh in whole øre, so the PDF attachment always matches the Fiken invoice line.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from zaptec_api import ChargeSession

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
    sessions: list[ChargeSession]
    total_kwh: float


def previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def validate_period(year: int, month: int) -> None:
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        raise ValidationError("Ugyldig periode")


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of a calendar month, in UTC.

    Sessions belong to the month in which they ended, as before.
    """
    validate_period(year, month)
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def total_kwh(sessions: list[ChargeSession]) -> float:
    return round(sum(s.energy for s in sessions), 3)


def _pdf_time(value: datetime | None) -> str:
    return value.isoformat() if value else "N/A"


def pdf_sessions(sessions: list[ChargeSession]) -> list[dict[str, Any]]:
    """Session rows for the PDF, in the shape and order the PDF has always
    used: newest first, times in UTC, energy as reported by Zaptec."""
    ordered = sorted(sessions, key=lambda s: s.start or datetime.min, reverse=True)
    return [
        {
            "StartDateTime": _pdf_time(s.start),
            "EndDateTime": _pdf_time(s.end),
            "DeviceName": s.device_name or "N/A",
            "Energy": s.energy,
        }
        for s in ordered
    ]


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
