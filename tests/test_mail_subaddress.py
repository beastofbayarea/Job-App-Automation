"""Unit tests for the alternate dynamic subaddress generator."""

import pytest
from job_application_automation.mail.pool import generate_subaddress_email


def test_generate_subaddress_email_basic():
    result = generate_subaddress_email("user@gmail.com", "Anthropic")
    assert result == "user+anthropic@gmail.com"


def test_generate_subaddress_email_with_special_chars():
    result = generate_subaddress_email("john.doe@domain.co.uk", "Acme & Co., Inc.")
    assert result == "john.doe+acme__co_inc@domain.co.uk"


def test_generate_subaddress_email_with_existing_plus():
    result = generate_subaddress_email("user+existing@gmail.com", "OpenAI", tag="lead")
    assert result == "user+openai_lead@gmail.com"


def test_generate_subaddress_email_invalid():
    with pytest.raises(ValueError, match="Invalid base email"):
        generate_subaddress_email("not_an_email", "Company")
