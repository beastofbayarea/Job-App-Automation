from scripts.prune_inactive_lever_failed_json import identity


def test_identity_supports_global_and_eu_boards() -> None:
    assert identity("https://jobs.lever.co/acme/123") == ("global", "acme", "123")
    assert identity("https://jobs.eu.lever.co/acme/123") == ("eu", "acme", "123")
    assert identity("https://example.com/acme/123") is None
