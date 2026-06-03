"""Best-effort quantity scaling for serving-size changes.

Only the leading amount of a quantity string is scaled (e.g. "200" in "200",
"1 1/2" in "1 1/2", both ends of "3-4"). Embedded numbers like the "14" in
"1 (14 oz can)" are left alone, and vague amounts ("a handful", "to taste")
pass through untouched. Fractions are kept as kitchen-friendly fractions.
"""

import re
from fractions import Fraction

# A number: mixed ("1 1/2"), bare fraction ("3/4"), or decimal/integer. Order
# matters — the fraction forms must be tried before the bare-integer form, or
# "3/4" would match only its leading "3".
_NUMBER = r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?"
# Groups: 1 = first number, 2/4 = whitespace around an optional range separator,
# 3 = the separator itself, 5 = the second number of a range.
_LEADING = re.compile(
    rf"^\s*({_NUMBER})(\s*)(-|to|–)?(\s*)({_NUMBER})?", re.IGNORECASE
)


def parse_base_servings(servings) -> int | None:
    """Pull a base serving count from a recipe's servings text ('4', '4-6')."""
    if servings is None:
        return None
    match = re.search(r"\d+", str(servings))
    return int(match.group()) if match else None


def scale_quantity(quantity, factor: float):
    """Scale the leading amount of a quantity string by factor. Vague or
    unparseable quantities are returned unchanged."""
    if quantity is None or factor == 1:
        return quantity
    text = str(quantity)
    match = _LEADING.match(text)
    if not match:
        return quantity  # vague: "a handful", "to taste"

    mult = Fraction(factor).limit_denominator(1000)
    low = _parse_number(match.group(1)) * mult
    if match.group(5):  # a range like "3-4" / "3 to 4", spacing preserved
        high = _parse_number(match.group(5)) * mult
        return (
            f"{_format_amount(low)}{match.group(2)}{match.group(3)}{match.group(4)}"
            f"{_format_amount(high)}{text[match.end():]}"
        )
    # Single amount: replace only the leading number, keep everything after it
    # (including any whitespace) untouched.
    return f"{_format_amount(low)}{text[match.end(1):]}"


def _parse_number(token: str) -> Fraction:
    token = token.strip()
    if " " in token:  # mixed number "1 1/2"
        whole, frac = token.split(None, 1)
        return Fraction(int(whole)) + Fraction(frac)
    if "/" in token:
        return Fraction(token)
    return Fraction(token)


def _format_amount(value: Fraction) -> str:
    value = Fraction(value).limit_denominator(8)
    if value.denominator == 1:
        return str(value.numerator)
    whole = value.numerator // value.denominator
    remainder = value - whole
    if whole and remainder:
        return f"{whole} {remainder.numerator}/{remainder.denominator}"
    if whole:
        return str(whole)
    return f"{value.numerator}/{value.denominator}"
