from scripts.prune_inactive_ashby_failed_json import ashby_identity, check_urls


def test_ashby_identity_extracts_board_and_job_id() -> None:
    assert ashby_identity("https://jobs.ashbyhq.com/acme/job-123?ref=x") == (
        "acme",
        "job-123",
    )
    assert ashby_identity("https://example.com/acme/job-123") is None


def test_check_urls_preserves_unknown_and_classifies_live_and_closed(monkeypatch) -> None:
    def fake_fetch(board: str, timeout: float):
        assert timeout == 3
        if board == "unknown":
            return None, "request_failed", None
        return {"live-id"}, "active_board", 200

    monkeypatch.setattr("scripts.prune_inactive_ashby_failed_json.fetch_active_ids", fake_fetch)
    urls = [
        "https://jobs.ashbyhq.com/acme/live-id",
        "https://jobs.ashbyhq.com/acme/closed-id",
        "https://jobs.ashbyhq.com/unknown/job-id",
    ]

    checks = check_urls(urls, timeout=3, workers=2)

    assert checks[urls[0]].status == "live"
    assert checks[urls[1]].status == "closed"
    assert checks[urls[2]].status == "unknown"
