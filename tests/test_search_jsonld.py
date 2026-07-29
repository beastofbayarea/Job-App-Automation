"""Unit tests for jsonld.py Schema.org extraction."""

import json
from job_application_automation.search.jsonld import (
    JsonLdExtractor,
    TextExtractor,
    clean_whitespace,
    strip_html,
    extract_jsonld_objects,
    is_jobposting_object,
    jsonld_location,
    jsonld_salary,
)


def test_clean_whitespace_and_strip_html() -> None:
    assert clean_whitespace("  Hello   World \n\t ") == "Hello World"
    assert clean_whitespace(None) == ""

    assert strip_html("<p>Hello <b>World</b></p>") == "Hello World"
    assert strip_html(None) == ""
    assert strip_html("&lt;p&gt;Escaped HTML&lt;/p&gt;") == "Escaped HTML"


def test_json_ld_extractor() -> None:
    html_content = """
    <html>
      <head>
        <script type="application/ld+json">
          {"@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer"}
        </script>
      </head>
    </html>
    """
    objects = list(extract_jsonld_objects(html_content))
    assert len(objects) == 1
    assert objects[0]["title"] == "Software Engineer"
    assert is_jobposting_object(objects[0]) is True


def test_jsonld_location_and_salary() -> None:
    job_obj = {
        "jobLocation": {
            "address": {
                "addressLocality": "San Francisco",
                "addressRegion": "CA",
            }
        },
        "jobLocationType": "TELECOMMUTE",
    }
    loc = jsonld_location(job_obj)
    assert "San Francisco, CA" in loc
    assert "Remote" in loc

    salary_obj = {"currency": "USD", "value": {"minValue": 120000, "maxValue": 150000, "unitText": "YEAR"}}
    sal = jsonld_salary(salary_obj)
    assert isinstance(sal, str)
