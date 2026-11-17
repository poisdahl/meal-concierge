"""Exact culinary quantities shared by normalization and grocery preparation."""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import re
from typing import Any, Mapping
import unicodedata


MAX_NUMERATOR = 10**15
MAX_DENOMINATOR = 10**12

# A bare cup, fluid ounce or volume-to-mass conversion is deliberately absent:
# the source must supply its measurement convention or density. Culinary counts
# such as pinches retain their own dimensions, never a physical conversion.
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
    **{unit: ("clove", Fraction(1)) for unit in ("fedd", "clove", "cloves")},
    **{unit: ("stalk", Fraction(1)) for unit in ("stilk", "stilker", "stalk", "stalks")},
    **{unit: ("package", Fraction(1)) for unit in ("pk", "pakke", "pakker", "package", "packages")},
    **{unit: ("drop", Fraction(1)) for unit in ("drop", "drops", "dråpe", "dråper")},
    **{unit: ("pinch", Fraction(1)) for unit in ("pinch", "pinches", "klype", "klyper")},
    **{unit: ("handful", Fraction(1)) for unit in ("handful", "handfuls", "håndfull", "håndfuller", "neve", "never")},
    **{unit: ("slice", Fraction(1)) for unit in ("slice", "slices", "skive", "skiver")},
    **{unit: ("bunch", Fraction(1)) for unit in ("bunch", "bunches", "bunt", "bunter", "knippe", "knipper")},
    **{unit: ("pot", Fraction(1)) for unit in ("pot", "pots", "potte", "potter")},
    **{unit: ("sheet", Fraction(1)) for unit in ("sheet", "sheets", "ark")},
    **{unit: ("thumb", Fraction(1)) for unit in ("thumb", "thumbs", "tommel", "tomler")},
    "oz": ("g", Fraction(45359237, 1600000)),
    "lb": ("g", Fraction(45359237, 100000)),
    **{unit: ("ml", Fraction(250)) for unit in ("metric cup", "metrisk kopp")},
    **{unit: ("ml", Fraction(473176473, 2000000)) for unit in ("us cup", "amerikansk kopp")},
    **{unit: ("ml", Fraction(473176473, 500000)) for unit in ("us liquid quart", "amerikansk væskekvart")},
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


def display_amount(value: Any, unit: Any = None, language: str | None = None) -> str | None:
    """Exact presentation only: mixed counts, short decimals and localized units.

    Never round, change the measurement convention or infer an unknown amount.
    Callers add this as display_amount; canonical amount/raw/quantity stay intact.
    """
    try:
        number = read_quantity(value, legacy_float=True)
    except ValueError:
        return None
    family = language.split("-")[0] if isinstance(language, str) else "nb"
    name = normalized_unit(unit)
    dimension = UNITS.get(name, (None, None))[0]
    counts = {"count", "clove", "stalk", "package", "drop", "pinch", "handful", "slice", "bunch", "pot", "sheet", "thumb"}
    whole, remainder = divmod(number.numerator, number.denominator)
    shown = str(whole) if not remainder else (f"{whole} " if whole else "") + f"{remainder}/{number.denominator}"
    if remainder and dimension not in counts:
        denominator, twos, fives = number.denominator, 0, 0
        while denominator % 2 == 0:
            denominator //= 2
            twos += 1
        while denominator % 5 == 0:
            denominator //= 5
            fives += 1
        places = max(twos, fives)
        if denominator == 1 and places <= 6:
            scaled = number.numerator * (10 ** places // number.denominator)
            digits = str(scaled).rjust(places + 1, "0")
            separator = "," if family in {"nb", "nn", "no", "sv", "da"} else "."
            shown = digits[:-places] + separator + digits[-places:]
    if family in {"en", "nb", "nn", "no"}:
        english = family == "en"
        labels = {
            "count": ("piece", "pieces") if english else ("stk", "stk"),
            "clove": ("clove", "cloves") if english else ("fedd", "fedd"),
            "stalk": ("stalk", "stalks") if english else ("stilk", "stilker"),
            "package": ("package", "packages") if english else ("pakke", "pakker"),
            "drop": ("drop", "drops") if english else ("dråpe", "dråper"),
            "pinch": ("pinch", "pinches") if english else ("klype", "klyper"),
            "handful": ("handful", "handfuls") if english else ("håndfull", "håndfuller"),
            "slice": ("slice", "slices") if english else ("skive", "skiver"),
            "bunch": ("bunch", "bunches") if english else ("bunt", "bunter"),
            "pot": ("pot", "pots") if english else ("potte", "potter"),
            "sheet": ("sheet", "sheets") if english else ("ark", "ark"),
            "thumb": ("thumb", "thumbs") if english else ("tommel", "tomler"),
        }
        if dimension in labels:
            name = labels[dimension][number > 1]
        elif name in UNITS:
            if UNITS[name] == ("ml", Fraction(5)):
                name = "tsp" if english else "ts"
            elif UNITS[name] == ("ml", Fraction(15)):
                name = "tbsp" if english else "ss"
            elif name in {"metric cup", "metrisk kopp"}:
                name = "metric cup" if english else "metrisk kopp"
            elif name in {"us cup", "amerikansk kopp"}:
                name = "US cup" if english else "amerikansk kopp"
            elif name in {"us liquid quart", "amerikansk væskekvart"}:
                name = "US liquid quart" if english else "amerikansk væskekvart"
            elif name not in {"oz", "lb"}:
                name = {Fraction(1, 1000): "mg", Fraction(1): dimension,
                        Fraction(10): "cl", Fraction(100): "dl",
                        Fraction(1000): "kg" if dimension == "g" else "l"}.get(UNITS[name][1], name)
    return (shown + " " + name).strip()


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
