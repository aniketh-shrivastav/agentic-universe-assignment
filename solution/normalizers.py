from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from solution.schema_loader import FieldDef, SchemaDef


def normalize_phone(raw: str) -> Optional[str]:
    """
    An Indian mobile number:
    Strip every non-digit.
    If 12 digits starting with 91, drop the 91.
    If 11 digits starting with 0, drop the 0.
    The result is valid only if it is exactly 10 digits and starts with 6, 7, 8 or 9.
    Store canonically as +91XXXXXXXXXX.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def mask_phone(canonical_phone: str) -> str:
    """
    phone_masked: +91****** followed by the last 4 digits.
    """
    if not canonical_phone or len(canonical_phone) < 4:
        return ""
    return f"+91******{canonical_phone[-4:]}"


def normalize_date(raw: str) -> Optional[str]:
    """
    Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, and DD-Mon-YYYY (e.g. 01-Dec-2026).
    Dates are day-first.
    Impossible dates (like 31/02) are invalid.
    Returns canonical YYYY-MM-DD string, or None if invalid.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
    ]

    for fmt in formats:
        try:
            # For %d-%b-%Y, title-case to support case-insensitive month names like '01-dec-2026' or '01-DEC-2026'
            target_str = s.title() if "%b" in fmt else s
            dt = datetime.strptime(target_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_decimal(raw: str) -> Optional[float]:
    """
    Optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
    Commas are thousands separators. Must be >= 0. Round to 2 places.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Remove optional currency prefix: ₹, Rs, Rs., INR (case-insensitive)
    s_clean = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE).strip()
    # Remove thousands separator commas
    s_clean = s_clean.replace(",", "")

    try:
        dec = Decimal(s_clean)
        if dec < Decimal("0"):
            return None
        # Round to 2 decimal places using standard half-up
        rounded = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(rounded)
    except (InvalidOperation, ValueError):
        return None


def normalize_enum(raw: str, allowed_values: Optional[List[str]]) -> Optional[str]:
    """
    Compared case-insensitively against values and stored lower-case.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if allowed_values:
        allowed_lower = [v.lower() for v in allowed_values]
        if s in allowed_lower:
            return s
        return None
    return s


def normalize_string(raw: str) -> Optional[str]:
    """
    The trimmed value. An empty value counts as missing.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def validate_and_normalize_field(
    raw_val: Any,
    field_def: FieldDef,
) -> Tuple[Optional[Any], bool]:
    """
    Validates and normalises a single field value.
    Returns (normalized_value, is_valid).
    If raw_val is missing/empty string, returns (None, True).
    If present but invalid, returns (None, False).
    """
    if raw_val is None:
        return None, True
    
    val_str = str(raw_val).strip()
    if not val_str:
        return None, True

    f_type = field_def.type.lower()
    if f_type == "string":
        return normalize_string(val_str), True
    elif f_type == "phone":
        res = normalize_phone(val_str)
        return res, (res is not None)
    elif f_type == "date":
        res = normalize_date(val_str)
        return res, (res is not None)
    elif f_type == "decimal":
        res = normalize_decimal(val_str)
        return res, (res is not None)
    elif f_type == "enum":
        res = normalize_enum(val_str, field_def.values)
        return res, (res is not None)
    else:
        # Fallback to string
        return normalize_string(val_str), True
