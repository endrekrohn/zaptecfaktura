from datetime import date
from decimal import Decimal

import pytest

from billing import (
    ValidationError,
    draft_uuid,
    line_total_ore,
    month_range,
    nok_to_ore,
    parse_nok_per_kwh,
    pdf_filename,
    previous_month,
    total_kwh,
)
from invoice import format_norwegian_accounting


def test_previous_month_wraps_year():
    assert previous_month(date(2026, 1, 10)) == (2025, 12)
    assert previous_month(date(2026, 11, 10)) == (2026, 10)


def test_month_range_december():
    assert month_range(2025, 12) == ("2025-12-01T00:00:00Z", "2026-01-01T00:00:00Z")


@pytest.mark.parametrize("year,month", [(2026, 0), (2026, 13), (1999, 5)])
def test_month_range_rejects_invalid(year, month):
    with pytest.raises(ValidationError):
        month_range(year, month)


def test_price_parsing_and_rounding():
    assert parse_nok_per_kwh("1,255") == Decimal("1.255")
    assert nok_to_ore(Decimal("1.255")) == 126
    assert nok_to_ore(Decimal("1.50")) == 150
    for bad in ["-1", "abc", "1000", "nan"]:
        with pytest.raises(ValidationError):
            parse_nok_per_kwh(bad)


def test_line_total_and_kwh():
    assert total_kwh([{"Energy": 1.2345}, {"Energy": 2}, {}]) == 3.234
    assert line_total_ore(3.234, 150) == 485  # 485.1 øre


def test_draft_uuid_is_stable_per_installation_and_month():
    a = draft_uuid("inst-1", 2026, 10)
    assert a == draft_uuid("inst-1", 2026, 10)
    assert a != draft_uuid("inst-1", 2026, 11)
    assert a != draft_uuid("inst-2", 2026, 10)


def test_pdf_filename_is_safe():
    assert pdf_filename(2026, 3, 'Gata 1/B: "Garasje"') == "2026_03_grunnlag_Gata_1_B___Garasje_.pdf"


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0,00"), (1234567.891, "1 234 567,89"), (0.995, "1,00"), (-1500.5, "-1 500,50")],
)
def test_format_norwegian_accounting(value, expected):
    assert format_norwegian_accounting(value) == expected
