"""Versioned, dependency-injected persistence for search discovery state.

The command module retains its long-standing ``Board`` and ``SearchCandidate``
classes.  This module deliberately operates on factories and small callbacks
instead of importing those classes, which keeps cache migration testable without
pulling in HTTP, DDGS, or CLI dependencies.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable


DISCOVERY_CACHE_VERSION = 2


def _payload_sections(payload: Any) -> tuple[Any, Any, Any, Any] | None:
    """Return compatible cache sections for version 0 through version 2 data."""
    if isinstance(payload, list):
        # Version 0 stored a bare board list.
        return payload, [], {}, []
    if isinstance(payload, dict):
        return (
            payload.get("boards", []),
            payload.get("candidates", []),
            payload.get("board_status", {}),
            payload.get("query_history", []),
        )
    return None


def decode_discovery_cache(
    payload: Any,
    *,
    make_cache: Callable[..., Any],
    board_from_cache_value: Callable[[Any], Any | None],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    clean_text: Callable[[Any], str],
) -> Any:
    """Decode legacy and current discovery cache payloads into caller models.

    Invalid individual entries are ignored, matching the forgiving behavior of
    the original cache reader.  A non-cache payload returns an empty cache.
    """
    sections = _payload_sections(payload)
    if sections is None:
        return make_cache()
    board_items, candidate_items, board_status, query_history = sections

    boards: set[Any] = set()
    if isinstance(board_items, list):
        for item in board_items:
            board = board_from_cache_value(item)
            if board is not None:
                boards.add(board)

    candidates_by_board: dict[str, list[Any]] = {}
    if isinstance(candidate_items, list):
        for item in candidate_items:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            board = board_from_cache_value(item.get("board"))
            if board is None:
                continue
            boards.add(board)
            provenance = item.get("provenance", [])
            add_candidate(
                candidates_by_board,
                make_candidate(
                    url=clean_text(item.get("url")),
                    title=clean_text(item.get("title")),
                    snippet=clean_text(item.get("snippet")),
                    board=board,
                    provenance=[clean_text(value) for value in provenance if value]
                    if isinstance(provenance, list)
                    else [],
                    first_seen_at=clean_text(item.get("first_seen_at")),
                    last_seen_at=clean_text(item.get("last_seen_at")),
                ),
            )

    return make_cache(
        boards=boards,
        candidates_by_board=candidates_by_board,
        board_status=board_status if isinstance(board_status, dict) else {},
        query_history=query_history if isinstance(query_history, list) else [],
    )


def load_discovery_cache(
    path: Path,
    *,
    make_cache: Callable[..., Any],
    decode: Callable[[Any], Any],
    on_error: Callable[[Exception], None],
) -> Any:
    """Read one cache file while isolating filesystem/JSON failures at the edge."""
    if not path.exists():
        return make_cache()
    try:
        return decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        on_error(exc)
        return make_cache()


def discovery_cache_payload(cache: Any, *, updated_at: str) -> dict[str, Any]:
    """Encode cache state in the stable version-2 on-disk schema."""
    ordered_boards = sorted(
        cache.boards,
        key=lambda board: (board.platform, board.region, board.token.lower()),
    )
    candidates = sorted(
        (
            candidate.to_cache_dict()
            for bucket in cache.candidates_by_board.values()
            for candidate in bucket
        ),
        key=lambda candidate: (str(candidate["board"]), str(candidate["url"])),
    )
    return {
        "version": DISCOVERY_CACHE_VERSION,
        "updated_at": updated_at,
        "boards": [asdict(board) for board in ordered_boards],
        "candidates": candidates,
        "board_status": cache.board_status,
        "query_history": cache.query_history[-1000:],
    }


def save_discovery_cache(
    path: Path,
    cache: Any,
    *,
    updated_at: str,
    write_json: Callable[..., Any],
) -> None:
    """Atomically persist a discovery cache through the caller's storage adapter."""
    write_json(
        path,
        discovery_cache_payload(cache, updated_at=updated_at),
        indent=2,
        ensure_ascii=False,
    )
