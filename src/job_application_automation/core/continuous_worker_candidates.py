"""Candidate-state selection and confirmed-ledger indexes for ATS workers."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from .artifacts import read_json
from .identity import canonical_job_url


EXACT_CONFIRMED_STATUS = "SUBMITTED & CONFIRMED"
RESUMABLE_STATUSES = frozenset({"preparing", "documents_ready"})

CandidateT = TypeVar("CandidateT", bound=Mapping[str, Any])
CandidateKey = Callable[[CandidateT], str]
JobIdentity = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ConfirmedSubmission:
    """One exact-confirmed ledger record normalized for worker coordination."""

    identity: str
    canonical_url: str
    job_url: str
    company: str
    title: str
    applied_at: str


@dataclass(frozen=True, slots=True)
class ExactConfirmedLedgerIndex:
    """Provider-scoped exact-confirmed records keyed by a caller-defined identity."""

    records: Mapping[str, ConfirmedSubmission]

    @property
    def identities(self) -> frozenset[str]:
        return frozenset(self.records)

    def contains(self, identity: str) -> bool:
        return identity in self.records


@dataclass(frozen=True, slots=True)
class CandidateStatePools(Generic[CandidateT]):
    """Resumable candidates first, followed by never-attempted candidates."""

    resumable: tuple[CandidateT, ...]
    fresh: tuple[CandidateT, ...]


def load_exact_confirmed_ledger_index(
    path: Path,
    ats_platform: str,
    *,
    identity_for_url: JobIdentity = canonical_job_url,
) -> ExactConfirmedLedgerIndex:
    """Index only canonical ``SUBMITTED & CONFIRMED`` records for one provider."""
    if not path.is_file():
        return ExactConfirmedLedgerIndex(records={})
    payload: object = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("submission log root must be an object")

    records: dict[str, ConfirmedSubmission] = {}
    for value in payload.values():
        if not isinstance(value, Mapping):
            continue
        if str(value.get("status", "")).strip() != EXACT_CONFIRMED_STATUS:
            continue
        if str(value.get("ats", "")).strip().lower() != ats_platform:
            continue
        job_url = str(value.get("job_url", "")).strip()
        try:
            canonical_url = canonical_job_url(job_url)
            identity = identity_for_url(job_url)
        except ValueError:
            continue
        records[identity] = ConfirmedSubmission(
            identity=identity,
            canonical_url=canonical_url,
            job_url=job_url,
            company=str(value.get("company") or ""),
            title=str(value.get("role") or ""),
            applied_at=str(value.get("applied_at") or ""),
        )
    return ExactConfirmedLedgerIndex(records=records)


def partition_candidate_state(
    candidates: Sequence[CandidateT],
    state_records: Mapping[str, object],
    *,
    state_key: CandidateKey[CandidateT],
    identity: CandidateKey[CandidateT],
    confirmed_identities: Collection[str] = (),
    blocked_identities: Collection[str] = (),
) -> CandidateStatePools[CandidateT]:
    """Classify resumable and fresh work while excluding confirmed or peer-owned jobs."""
    confirmed = set(confirmed_identities)
    blocked = set(blocked_identities)
    by_state_key: dict[str, CandidateT] = {}
    for candidate in candidates:
        try:
            candidate_state_key = state_key(candidate)
            candidate_identity = identity(candidate)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not candidate_state_key
            or candidate_identity in confirmed
            or candidate_identity in blocked
        ):
            continue
        by_state_key[candidate_state_key] = candidate

    resumable: list[CandidateT] = []
    for candidate_state_key, record in state_records.items():
        resumable_candidate = by_state_key.get(candidate_state_key)
        if resumable_candidate is None or not isinstance(record, Mapping):
            continue
        if record.get("status") not in RESUMABLE_STATUSES:
            continue
        resumable.append(resumable_candidate)

    fresh = tuple(
        candidate
        for candidate_state_key, candidate in by_state_key.items()
        if candidate_state_key not in state_records
    )
    return CandidateStatePools(resumable=tuple(resumable), fresh=fresh)


def choose_resumable_or_fresh(
    pools: CandidateStatePools[CandidateT],
    *,
    choice: Callable[[Sequence[CandidateT]], CandidateT],
) -> CandidateT | None:
    """Resume deterministic in-progress work before sampling a fresh candidate."""
    if pools.resumable:
        return pools.resumable[0]
    return choice(pools.fresh) if pools.fresh else None
