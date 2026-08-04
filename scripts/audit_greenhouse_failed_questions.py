"""Inventory unanswered prompts from Greenhouse failed-record JSON exports."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def _questions(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"\s*\|\s*|\s*;\s*", str(value or ""))
    return [" ".join(part.split()) for part in parts if str(part).strip()]


def audit(data_dir: Path) -> dict[str, Any]:
    prompts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "files": set(), "applications": []}
    )
    failures_without_questions: list[dict[str, str]] = []
    file_counts: dict[str, int] = {}
    total_records = 0
    records_with_questions = 0
    question_occurrences = 0

    for path in sorted(data_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON list")
        file_counts[path.name] = len(payload)
        total_records += len(payload)
        for record in payload:
            if not isinstance(record, dict):
                continue
            questions = _questions(record.get("missing_required"))
            if questions:
                records_with_questions += 1
            else:
                failures_without_questions.append(
                    {
                        "file": path.name,
                        "company": str(record.get("company", "")),
                        "role": str(record.get("role", "")),
                        "status": str(record.get("status", "")),
                        "failure_reason": str(record.get("failure_reason", "")),
                        "job_url": str(record.get("job_url", "")),
                    }
                )
            for question in questions:
                question_occurrences += 1
                entry = prompts[question]
                entry["count"] += 1
                entry["files"].add(path.name)
                entry["applications"].append(
                    {
                        "company": str(record.get("company", "")),
                        "role": str(record.get("role", "")),
                        "job_url": str(record.get("job_url", "")),
                    }
                )

    exact_questions = []
    for question, entry in sorted(prompts.items(), key=lambda item: (-item[1]["count"], item[0])):
        exact_questions.append(
            {
                "question": question,
                "count": entry["count"],
                "files": sorted(entry["files"]),
                "applications": entry["applications"],
            }
        )
    return {
        "summary": {
            "files": len(file_counts),
            "records": total_records,
            "records_with_questions": records_with_questions,
            "records_without_questions": len(failures_without_questions),
            "question_occurrences": question_occurrences,
            "unique_exact_questions": len(exact_questions),
            "records_by_file": file_counts,
        },
        "exact_questions": exact_questions,
        "failures_without_questions": failures_without_questions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data/application-queues/greenhouse"))
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.data_dir)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with args.csv_output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("question", "count", "files", "applications"))
        writer.writeheader()
        for entry in report["exact_questions"]:
            writer.writerow(
                {
                    "question": entry["question"],
                    "count": entry["count"],
                    "files": " | ".join(entry["files"]),
                    "applications": " | ".join(
                        f"{item['company']} — {item['role']} — {item['job_url']}"
                        for item in entry["applications"]
                    ),
                }
            )
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
