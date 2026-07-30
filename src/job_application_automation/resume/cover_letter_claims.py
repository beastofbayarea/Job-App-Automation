"""Claim-ID evidence checks against the tagged resume source.

An LLM-returned ``evidence_claim_ids`` entry is trustworthy only if it names a
``[CLAIM <id>]`` already present in the candidate's tagged source material.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterable, Mapping, Sequence


def known_claim_ids(experience: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return every tagged claim ID available across tagged experience entries."""
    ids: set[str] = set()
    for entry in experience:
        for claim in entry.get("claims", []) or []:
            claim_id = str(claim.get("id", "")).strip()
            if claim_id:
                ids.add(claim_id)
    return ids


def validate_claim_ids(evidence_claim_ids: Iterable[str], known_ids: set[str]) -> list[str]:
    """Return the subset of ``evidence_claim_ids`` absent from ``known_ids``."""
    return [str(claim_id) for claim_id in evidence_claim_ids if str(claim_id) not in known_ids]
