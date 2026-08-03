from scripts.prune_inactive_smartrecruiters_workable_json import identity


def test_provider_identities() -> None:
    assert identity("https://jobs.smartrecruiters.com/Acme/123-role", "smartrecruiters") == ("Acme", "123")
    assert identity("https://apply.workable.com/acme/j/ABC123", "workable") == ("acme", "ABC123")
    assert identity("https://apply.workable.com/j/ABC123", "workable") is None
