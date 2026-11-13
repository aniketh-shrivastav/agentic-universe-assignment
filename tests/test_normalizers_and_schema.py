import pytest
from pathlib import Path
from solution.schema_loader import normalize_header, SchemaRegistry
from solution.normalizers import (
    normalize_phone,
    mask_phone,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_string,
    validate_and_normalize_field,
    validate_lead_row,
)
from solution.schema_loader import FieldDef

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


class TestHeaderNormalization:
    def test_normalize_header_variations(self):
        assert normalize_header("Customer Name") == "customer name"
        assert normalize_header("customer_name") == "customer name"
        assert normalize_header("  CUSTOMER___NAME  ") == "customer name"
        assert normalize_header("mobile_no") == "mobile no"
        assert normalize_header("Policy   No") == "policy no"

    def test_schema_loader_matching(self):
        reg = SchemaRegistry(SCHEMA_DIR)
        schema = reg.get_schema("motor_renewal")
        assert schema is not None
        assert schema.workflow_id == "motor_renewal"

        # Check matching by canonical name
        f_name = schema.match_header("customer_name")
        assert f_name is not None
        assert f_name.name == "customer_name"

        # Check matching by alias
        f_name_alias = schema.match_header("insured name")
        assert f_name_alias is not None
        assert f_name_alias.name == "customer_name"

        f_phone_alias = schema.match_header("mobile no")
        assert f_phone_alias is not None
        assert f_phone_alias.name == "phone"

        # Unknown header
        assert schema.match_header("random_unrelated_column") is None

    def test_disposition_loader(self):
        reg = SchemaRegistry(SCHEMA_DIR)
        disp = reg.get_disposition("motor_renewal", "renewed")
        assert disp is not None
        assert disp.is_terminal is True
        assert disp.is_success is True

        disp_cb = reg.get_disposition("motor_renewal", "callback_requested")
        assert disp_cb is not None
        assert disp_cb.is_terminal is False
        assert disp_cb.is_success is False


class TestPhoneNormalization:
    def test_valid_indian_phones(self):
        # 10 digits starting with 6, 7, 8, 9
        assert normalize_phone("9876543210") == "+919876543210"
        assert normalize_phone("8123456789") == "+918123456789"
        assert normalize_phone("7123456789") == "+917123456789"
        assert normalize_phone("6123456789") == "+916123456789"

        # With 91 prefix (12 digits)
        assert normalize_phone("919876543210") == "+919876543210"
        assert normalize_phone("+91 98765 43210") == "+919876543210"
        assert normalize_phone("91-98765-43210") == "+919876543210"
        assert normalize_phone("(+91) 9876543210") == "+919876543210"

        # With 0 prefix (11 digits)
        assert normalize_phone("09876543210") == "+919876543210"

    def test_invalid_phones(self):
        assert normalize_phone("5123456789") is None  # starts with 5
        assert normalize_phone("987654321") is None   # 9 digits
        assert normalize_phone("987654321000") is None  # too many digits without 91 prefix
        assert normalize_phone("022-2432-1234") is None # landline
        assert normalize_phone("+1 415 555 0134") is None # US number
        assert normalize_phone("N/A") is None
        assert normalize_phone("") is None
        assert normalize_phone(None) is None

    def test_mask_phone(self):
        assert mask_phone("+919876543210") == "+91******3210"


class TestDateNormalization:
    def test_valid_dates(self):
        # YYYY-MM-DD
        assert normalize_date("2026-12-01") == "2026-12-01"
        # DD/MM/YYYY (day first: 01/12 is Dec 1)
        assert normalize_date("01/12/2026") == "2026-12-01"
        # DD-MM-YYYY
        assert normalize_date("01-12-2026") == "2026-12-01"
        # DD-Mon-YYYY (case-insensitive month)
        assert normalize_date("01-Dec-2026") == "2026-12-01"
        assert normalize_date("01-dec-2026") == "2026-12-01"
        assert normalize_date("01-DEC-2026") == "2026-12-01"

    def test_invalid_dates(self):
        # Impossible date
        assert normalize_date("31/02/2026") is None
        assert normalize_date("2026-02-30") is None
        assert normalize_date("invalid-date") is None
        assert normalize_date("") is None
        assert normalize_date(None) is None


class TestDecimalNormalization:
    def test_valid_decimals(self):
        assert normalize_decimal("1500") == 1500.0
        assert normalize_decimal("1,500.50") == 1500.50
        assert normalize_decimal("₹ 12,345.678") == 12345.68
        assert normalize_decimal("Rs. 500") == 500.0
        assert normalize_decimal("rs 99.999") == 100.0
        assert normalize_decimal("INR 2500.1") == 2500.1
        assert normalize_decimal("0") == 0.0

    def test_invalid_decimals(self):
        assert normalize_decimal("-500") is None
        assert normalize_decimal("₹ -100") is None
        assert normalize_decimal("free") is None
        assert normalize_decimal("") is None
        assert normalize_decimal(None) is None


class TestEnumNormalization:
    def test_enums(self):
        allowed = ["en", "hi", "ta", "kn", "te", "mr"]
        assert normalize_enum("EN", allowed) == "en"
        assert normalize_enum("hi", allowed) == "hi"
        assert normalize_enum("  Ta  ", allowed) == "ta"
        assert normalize_enum("fr", allowed) is None
        assert normalize_enum("", allowed) is None
        assert normalize_enum(None, allowed) is None


class TestRowValidation:
    @pytest.fixture
    def schema(self):
        reg = SchemaRegistry(SCHEMA_DIR)
        return reg.get_schema("motor_renewal")

    def test_valid_row(self, schema):
        raw = {
            "customer_name": "Aarav Sharma",
            "phone": "9876543210",
            "policy_number": "POL-12345",
            "policy_expiry": "01-Dec-2026",
            "premium_amount": "₹ 15,000.50",
            "preferred_language": "HI",
        }
        clean, reason, warnings = validate_lead_row(raw, schema)
        assert reason is None
        assert warnings == 0
        assert clean["customer_name"] == "Aarav Sharma"
        assert clean["phone"] == "+919876543210"
        assert clean["policy_number"] == "POL-12345"
        assert clean["policy_expiry"] == "2026-12-01"
        assert clean["premium_amount"] == 15000.50
        assert clean["preferred_language"] == "hi"

    def test_missing_required_field(self, schema):
        raw = {
            "customer_name": "",  # missing required
            "phone": "9876543210",
            "policy_number": "POL-12345",
        }
        clean, reason, warnings = validate_lead_row(raw, schema)
        assert clean is None
        assert reason == "missing_required:customer_name"
        assert warnings == 0

    def test_invalid_phone(self, schema):
        raw = {
            "customer_name": "Aarav Sharma",
            "phone": "12345",  # invalid
            "policy_number": "POL-12345",
        }
        clean, reason, warnings = validate_lead_row(raw, schema)
        assert clean is None
        assert reason == "invalid_phone"
        assert warnings == 0

    def test_schema_order_precedence(self, schema):
        # Both customer_name is missing and phone is invalid.
        # customer_name is first in schema order, so missing_required:customer_name must win.
        raw = {
            "customer_name": "",
            "phone": "12345",
            "policy_number": "POL-12345",
        }
        clean, reason, warnings = validate_lead_row(raw, schema)
        assert clean is None
        assert reason == "missing_required:customer_name"

    def test_optional_invalid_generates_warning(self, schema):
        raw = {
            "customer_name": "Aarav Sharma",
            "phone": "9876543210",
            "policy_number": "POL-12345",
            "policy_expiry": "31/02/2026",  # impossible date -> null + warning
            "premium_amount": "-500",       # negative -> null + warning
            "preferred_language": "french", # invalid enum -> null + warning
        }
        clean, reason, warnings = validate_lead_row(raw, schema)
        assert reason is None
        assert warnings == 3
        assert clean["policy_expiry"] is None
        assert clean["premium_amount"] is None
        assert clean["preferred_language"] is None


