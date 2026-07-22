from scripts.check_repo_hygiene import contains_possible_secret


def test_hygiene_distinguishes_explicit_fixtures_from_possible_live_keys() -> None:
    assert not contains_possible_secret('api_key="fixture-key-not-a-real-secret"')
    assert not contains_possible_secret('api_key="p6-plaintext-secret-a"')
    assert contains_possible_secret("api_" + 'key="production-looking-value"')
    assert contains_possible_secret("s" + "k-abcdefghijklmnop")
