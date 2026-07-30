from __future__ import annotations

import random
import string
from job_application_automation.core.engine_shared import mask_email
from job_application_automation.engines.ashby import extract_lowest_salary
from job_application_automation.engines.ashby_sections import normalize_question_text


def random_string(length: int = 20) -> str:
    chars = (
        string.ascii_letters
        + string.digits
        + string.punctuation
        + " \t\n\u00a0\u2022\u20ac\u00a3\u20b9\u4e2d\u6587\u062f\u0628\u064a"
    )
    return "".join(random.choice(chars) for _ in range(length))


def test_fuzz_extract_lowest_salary() -> None:
    random.seed(42)
    for _ in range(300):
        s_text = random_string(random.randint(0, 50))
        loc_text = random_string(random.randint(0, 30))
        res = extract_lowest_salary(s_text, loc_text)
        assert isinstance(res, str)
        assert len(res) > 0


def test_fuzz_normalize_question_text() -> None:
    random.seed(42)
    for _ in range(300):
        q_text = random_string(random.randint(0, 100))
        res = normalize_question_text(q_text)
        assert isinstance(res, str)


def test_fuzz_mask_email() -> None:
    random.seed(42)
    for _ in range(300):
        email_candidate = random_string(random.randint(0, 40))
        res = mask_email(email_candidate)
        assert isinstance(res, str)
