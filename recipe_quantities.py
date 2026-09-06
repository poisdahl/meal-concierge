"""Exact culinary quantities shared by normalization and grocery preparation."""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import re
from typing import Any, Mapping
import unicodedata


MAX_NUMERATOR = 10**15
MAX_DENOMINATOR = 10**12

# A bare cup, fluid ounce, pinch or volume-to-mass conversion is deliberately
# absent: the source must supply its measurement convention or density.
UNITS = {
    "mg": ("g", Fraction(1, 1000)),
    **{unit: ("g", Fraction(1)) for unit in ("g", "gram", "grams", "gramme", "grammes")},
    **{unit: ("g", Fraction(1000)) for unit in ("kg", "kilogram", "kilograms")},
    **{unit: ("ml", Fraction(1)) for unit in ("ml", "milliliter", "milliliters", "millilitre", "millilitres")},
    "cl": ("ml", Fraction(10)),
    "dl": ("ml", Fraction(100)),
    **{unit: ("ml", Fraction(1000)) for unit in ("l", "liter", "liters", "litre", "litres")},
    **{unit: ("ml", Fraction(5)) for unit in ("ts", "tsp", "teaspoon", "teaspoons", "teskje", "teskjeer")},
    **{unit: ("ml", Fraction(15)) for unit in ("ss", "tbsp", "tablespoon", "tablespoons", "spiseskje", "spiseskjeer")},
    **{unit: ("count", Fraction(1)) for unit in ("stk", "stykk", "count", "piece", "pieces")},
    "oz": ("g", Fraction(45359237, 1600000)),
    "lb": ("g", Fraction(45359237, 100000)),
    "metric cup": ("ml", Fraction(250)),
    "us cup": ("ml", Fraction(473176473, 2000000)),
}


def normalized_unit(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold() if isinstance(value, str) else ""


def bounded_fraction(value: Fraction) -> Fraction:
    if value <= 0 or value.numerator > MAX_NUMERATOR or value.denominator > MAX_DENOMINATOR:
        raise ValueError("quantity exceeds the positive exact fraction limits")
    return value


def read_quantity(value: Any, *, legacy_float: bool = False) -> Fraction:
    """Read a bounded exact amount; old floats have one documented recovery boundary."""
    if isinstance(value, Mapping):
        if set(value) != {"numerator", "denominator"}:
            raise ValueError("quantity needs numerator and denominator")
        n, d = value["numerator"], value["denominator"]
        if type(n) is not int or type(d) is not int or not 0 < n <= MAX_NUMERATOR or not 0 < d <= MAX_DENOMINATOR:
            raise ValueError("quantity fraction is invalid or too large")
        return bounded_fraction(Fraction(n, d))
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("quantity must be a positive number, decimal string or fraction")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("quantity must be finite")
    text = str(value)
    if len(text) > 80:
        raise ValueError("quantity is too long")
    try:
        decimal = Decimal(text)
        if not decimal.is_finite() or decimal <= 0 or not -12 <= decimal.adjusted() <= 15:
            raise ValueError("quantity is outside the supported range")
        result = Fraction(decimal)
    except InvalidOperation as exc:
        raise ValueError("quantity must be an exact decimal or fraction object") from exc
    if legacy_float and isinstance(value, float) and (result.numerator > MAX_NUMERATOR or result.denominator > MAX_DENOMINATOR):
        # Recover repeating decimal artifacts in existing frozen menus. This is
        # only the legacy numeric boundary, never the schema-2 calculation path.
        candidate = result.limit_denominator(10**9)
        if abs(candidate - result) <= Fraction(1, 10**12):
            result = candidate
    return bounded_fraction(result)


def quantity_json(value: Fraction) -> dict[str, int]:
    value = bounded_fraction(value)
    return {"numerator": value.numerator, "denominator": value.denominator}


def quantity_text(value: Any) -> str:
    number = read_quantity(value)
    return str(number.numerator) if number.denominator == 1 else f"{number.numerator}/{number.denominator}"


def parse_measure(value: str) -> tuple[dict[str, int] | None, str | None]:
    """Read an unambiguous source amount and unit without interpreting prose."""
    value = re.sub(r"(?<=\d)([¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", r" \1", value)
    text = unicodedata.normalize("NFKC", value).replace("⁄", "/").strip()
    match = re.fullmatch(r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)\s*([A-Za-z].*)", text)
    if not match:
        return None, None
    amount, unit = match.groups()
    unit = normalized_unit(unit)
    if unit not in UNITS:
        return None, unit
    try:
        if " " in amount:
            whole, part = amount.split()
            number = Fraction(whole) + Fraction(part)
        else:
            number = Fraction(amount.replace(",", "."))
        return quantity_json(number), unit
    except (ValueError, ZeroDivisionError):
        return None, unit
