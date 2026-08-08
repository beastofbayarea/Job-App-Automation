from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from job_application_automation.core.engine_shared import (
    generate_essay_answer,
    generate_essay_answers,
    is_sensitive_narrative_question,
)
from job_application_automation.resume import ai_client


class BatchedEssayGenerationTests(unittest.TestCase):
    def test_safe_questions_are_batched_and_sensitive_positions_stay_blank(self) -> None:
        questions = (
            "Why do you want this role?",
            "Explain your current work authorization and visa status.",
            "Describe a product launch you led.",
        )
        generated = ["Role-specific motivation.", "Distinct product-launch evidence."]

        with patch.object(ai_client, "call_essay_set_llm", return_value=generated) as call:
            answers = generate_essay_answers(
                questions,
                "Job description",
                "Example Co",
                "Product Manager",
                "Candidate evidence",
            )

        self.assertEqual(
            answers,
            ["Role-specific motivation.", "", "Distinct product-launch evidence."],
        )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(
            call.call_args.args[0],
            [questions[0], questions[2]],
        )

    def test_single_answer_helper_refuses_sensitive_prompt(self) -> None:
        question = "Tell us about your citizenship and sponsorship requirements."
        self.assertTrue(is_sensitive_narrative_question(question))

        with patch.object(ai_client, "call_essay_llm") as call:
            answer = generate_essay_answer(
                question,
                "Job description",
                "Example Co",
                "Product Manager",
                "Candidate evidence",
            )

        self.assertEqual(answer, "")
        call.assert_not_called()

    def test_batch_prompt_requires_concise_mece_answers(self) -> None:
        payload = {
            "answers": [
                {"question_index": 0, "answer": "First distinct answer."},
                {"question_index": 1, "answer": "Second distinct answer."},
            ]
        }
        with patch.object(ai_client, "ask_gemini", return_value=json.dumps(payload)) as ask:
            answers = ai_client.call_essay_set_llm(
                ["Why this role?", "Describe a launch."],
                "Job description",
                "Example Co",
                "Product Manager",
                "Candidate evidence",
            )

        self.assertEqual(answers, ["First distinct answer.", "Second distinct answer."])
        system_prompt = ask.call_args.kwargs["system"]
        self.assertIn("60-120 words", system_prompt)
        self.assertIn("mutually exclusive and collectively exhaustive", system_prompt)


if __name__ == "__main__":
    unittest.main()
