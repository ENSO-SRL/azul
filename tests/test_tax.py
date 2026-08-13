"""Tests for ITBIS helpers (tax-inclusive breakdown)."""

from __future__ import annotations

from decimal import Decimal

from app.utils.tax import included_itbis


def test_included_itbis_clean_case():
    # RD$590 total, ITBIS incluido = RD$90 (base RD$500)
    assert included_itbis(59000) == 9000


def test_included_itbis_rounds_half_up():
    # RD$500 total → 500 * 0.18/1.18 = 76.271... → RD$76.27
    assert included_itbis(50000) == 7627


def test_included_itbis_zero_and_negative():
    assert included_itbis(0) == 0
    assert included_itbis(-100) == 0


def test_included_itbis_custom_rate():
    # Tasa 0 → sin impuesto
    assert included_itbis(50000, rate=Decimal("0")) == 0
    # Tasa 10% incluida en 11000 → 1000
    assert included_itbis(11000, rate=Decimal("0.10")) == 1000


def test_included_itbis_is_a_subset_of_total():
    for total in (100, 1234, 5000, 59000, 999999):
        assert 0 <= included_itbis(total) < total
