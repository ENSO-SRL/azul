"""
ITBIS helpers.

Convención Azul: el campo ``Amount`` es el total a cobrar CON el impuesto
incluido, y ``Itbis`` es la porción de ese total que corresponde al impuesto
(no se suma — ya está dentro de Amount).

``included_itbis`` toma un total tax-inclusive y devuelve cuánto de ese total
es ITBIS, para declararlo a Azul / la factura:

    base * (1 + r) = total   →   itbis = total - base = total * r / (1 + r)

Con r = 0.18 (ITBIS RD 18%):
    total 59000 (RD$590)  → itbis 9000  (RD$90),  base 50000
    total 50000 (RD$500)  → itbis 7627  (RD$76.27)
"""

from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP


def itbis_rate() -> Decimal:
    """Tasa de ITBIS como Decimal. Configurable vía AZUL_ITBIS_RATE (default 0.18)."""
    try:
        return Decimal(os.getenv("AZUL_ITBIS_RATE", "0.18"))
    except Exception:
        return Decimal("0.18")


def included_itbis(total_cents: int, rate: Decimal | None = None) -> int:
    """Porción de ITBIS (en centavos) contenida en un total tax-inclusive.

    Redondeo HALF_UP al centavo. Devuelve 0 para totales <= 0.
    """
    if total_cents <= 0:
        return 0
    r = rate if rate is not None else itbis_rate()
    if r <= 0:
        return 0
    itbis = (Decimal(total_cents) * r / (Decimal(1) + r)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(itbis)
