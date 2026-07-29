from job_application_automation.engines.lever import _lever_semantic_answer


def test_numbered_language_questions_use_candidate_languages_in_order() -> None:
    profile = {"languages": ["English", "French", "Hindi"]}

    assert _lever_semantic_answer("Language 1", profile, {}) == "English"
    assert _lever_semantic_answer("Language 2", profile, {}) == "French"
    assert _lever_semantic_answer("Language 3", profile, {}) == "Hindi"
    assert _lever_semantic_answer("Language 4", profile, {}) is None


def test_nationality_question_uses_candidate_nationality() -> None:
    assert (
        _lever_semantic_answer("What is your nationality?", {"nationality": "Indian"}, {})
        == "Indian"
    )


def test_expected_compensation_range_uses_salary_policy() -> None:
    assert (
        _lever_semantic_answer(
            "What is your expected compensation range?",
            {},
            {"salary_expectation": "Negotiable"},
        )
        == "Negotiable"
    )
