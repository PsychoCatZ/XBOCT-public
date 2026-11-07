"""Money as integer minor units plus currency. Different currencies never mix."""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal

from xboct.domain.errors import ValidationFailed

CURRENCY_EXPONENT: dict[str, int] = {"RUB": 2, "USD": 2, "EUR": 2}
CURRENCY_SYMBOL: dict[str, str] = {"RUB": "₽", "USD": "$", "EUR": "€"}

_AMOUNT_RE = re.compile(r"^\d{1,12}([.,]\d{1,2})?$")


def check_currency(currency: str) -> str:
    code = currency.upper()
    if code not in CURRENCY_EXPONENT:
        raise ValidationFailed(f"Валюта {currency} не поддерживается", code="bad_currency")
    return code


def parse_major(amount: str, currency: str) -> int:
    """'1000' or '1000.50' (major units) -> minor units. Rejects signs and garbage."""
    text = amount.strip().replace(" ", "")
    if not _AMOUNT_RE.match(text):
        raise ValidationFailed(f"Непонятная сумма: {amount!r}", code="bad_amount")
    exponent = CURRENCY_EXPONENT[check_currency(currency)]
    value = Decimal(text.replace(",", ".")) * (Decimal(10) ** exponent)
    return int(value)


def minor_to_input(minor: int, currency: str) -> str:
    """Minor units -> text for a form field that parse_major reads back to the same value.

    Only insignificant zeros of the fractional part are dropped: 120000 -> "1200",
    120050 -> "1200,5", 0 -> "0".
    """
    exponent = CURRENCY_EXPONENT.get(currency, 2)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 10**exponent)
    text = f"{sign}{whole}"
    if exponent and frac:
        text += "," + f"{frac:0{exponent}d}".rstrip("0")
    return text


def format_minor(minor: int | Decimal, currency: str) -> str:
    exponent = CURRENCY_EXPONENT.get(currency, 2)
    value = Decimal(minor) / (Decimal(10) ** exponent)
    negative = value < 0
    value = abs(value).quantize(Decimal(1) / (Decimal(10) ** exponent), rounding=ROUND_DOWN)
    whole, _, frac = f"{value:f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", " ")
    text = grouped if not frac or set(frac) == {"0"} else f"{grouped},{frac}"
    symbol = CURRENCY_SYMBOL.get(currency, currency)
    return f"{'-' if negative else ''}{text} {symbol}"
