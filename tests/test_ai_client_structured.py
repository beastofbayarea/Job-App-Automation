"""Unit tests for constrained structured LLM decoding."""

from unittest.mock import patch
from job_application_automation.resume.ai_client import (
    call_resume_llm_structured,
    TailoredResumeSchema,
)


def test_call_resume_llm_structured_mock():
    mock_json = '{"header_tagline": "AI Engineer", "skills": {"python": ["pytest"]}, "experience": [{"company": "Acme", "role": "Lead", "duration": "2024", "location": "Remote", "bullets": ["Built RAG"]}]}'

    with patch("job_application_automation.resume.ai_client.ask_gemini", return_value=mock_json):
        result = call_resume_llm_structured("test prompt", schema_cls=TailoredResumeSchema)

    assert result["header_tagline"] == "AI Engineer"
    assert result["skills"] == {"python": ["pytest"]}
    assert len(result["experience"]) == 1
    assert result["experience"][0]["company"] == "Acme"
