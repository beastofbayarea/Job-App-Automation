from __future__ import annotations

import json
from job_application_automation.resume.cover_letter_claims import (
    known_claim_ids,
    validate_claim_ids,
)


MOCK_LLM_RESUME_RESPONSE = json.dumps(
    {
        "professional_summary": "Experienced AI Engineer specializing in Python, PyTorch, and NLP.",
        "skills": ["Python", "PyTorch", "Playwright", "FastAPI", "Docker"],
        "experience": [
            {
                "company": "Acme Corp",
                "title": "Senior AI Engineer",
                "bullets": [
                    "Built automated web extraction pipelines using Playwright.",
                    "Trained transformer models resulting in 25% efficiency gains.",
                ],
            }
        ],
    }
)


def test_llm_resume_response_schema_validation() -> None:
    data = json.loads(MOCK_LLM_RESUME_RESPONSE)
    assert "professional_summary" in data
    assert "skills" in data
    assert "experience" in data
    assert isinstance(data["experience"], list)
    assert data["experience"][0]["company"] == "Acme Corp"


def test_claim_id_verification_against_profile() -> None:
    experience = [{"company": "Acme Corp", "claims": [{"id": "CLAIM_1"}, {"id": "CLAIM_2"}]}]
    known = known_claim_ids(experience)
    assert known == {"CLAIM_1", "CLAIM_2"}

    # Valid claim IDs
    missing = validate_claim_ids(["CLAIM_1"], known)
    assert len(missing) == 0

    # Unverified claim ID
    missing_unverified = validate_claim_ids(["CLAIM_99"], known)
    assert missing_unverified == ["CLAIM_99"]
