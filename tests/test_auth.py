"""Credential resolution: which token is used, and from where.

This is the one module that decides what identity the tool acts as, and it had
no tests at all -- the precedence was documented in the docstring and asserted
nowhere. A wrong answer here does not fail loudly; it silently acts as the wrong
account, or as nobody.
"""

from __future__ import annotations

import json

import pytest

from openi_bridge import auth


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point HOME at a scratch dir and clear endpoint/token env vars."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENI_TOKEN", raising=False)
    monkeypatch.delenv("OPENI_ENDPOINT", raising=False)
    return tmp_path


def _write_token(home, payload):
    d = home / ".openi"
    d.mkdir(exist_ok=True)
    (d / "token.json").write_text(json.dumps(payload), encoding="utf-8")


class TestPrecedence:
    """Explicit argument > token file > environment."""

    def test_missing_everything_yields_no_token(self, fake_home):
        creds = auth.resolve_credentials()
        assert creds.token is None
        assert creds.has_token is False
        assert creds.endpoint == auth.DEFAULT_ENDPOINT

    def test_token_file_is_used(self, fake_home):
        _write_token(fake_home, {"token": "from-file"})
        assert auth.resolve_credentials().token == "from-file"

    def test_environment_is_the_fallback(self, fake_home, monkeypatch):
        monkeypatch.setenv("OPENI_TOKEN", "from-env")
        assert auth.resolve_credentials().token == "from-env"

    def test_file_beats_environment(self, fake_home, monkeypatch):
        _write_token(fake_home, {"token": "from-file"})
        monkeypatch.setenv("OPENI_TOKEN", "from-env")
        assert auth.resolve_credentials().token == "from-file"

    def test_explicit_argument_beats_both(self, fake_home, monkeypatch):
        _write_token(fake_home, {"token": "from-file"})
        monkeypatch.setenv("OPENI_TOKEN", "from-env")
        assert auth.resolve_credentials(token="explicit").token == "explicit"

    def test_endpoint_follows_the_same_order(self, fake_home, monkeypatch):
        _write_token(fake_home, {"token": "t", "endpoint": "https://file.example"})
        monkeypatch.setenv("OPENI_ENDPOINT", "https://env.example")
        assert auth.resolve_credentials().endpoint == "https://file.example"
        assert auth.resolve_credentials(endpoint="https://explicit.example").endpoint == (
            "https://explicit.example"
        )

    def test_trailing_slash_is_stripped(self, fake_home):
        creds = auth.resolve_credentials(endpoint="https://x.example/")
        assert creds.endpoint == "https://x.example"


class TestTokenFileHandling:
    def test_corrupt_json_falls_through_rather_than_crashing(self, fake_home, monkeypatch):
        d = fake_home / ".openi"
        d.mkdir()
        (d / "token.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("OPENI_TOKEN", "from-env")
        assert auth.resolve_credentials().token == "from-env"

    def test_missing_file_is_not_an_error(self, fake_home):
        assert auth.resolve_credentials().token is None

    def test_file_without_token_key_is_not_an_error(self, fake_home):
        _write_token(fake_home, {"endpoint": "https://x.example"})
        creds = auth.resolve_credentials()
        assert creds.token is None
        assert creds.endpoint == "https://x.example"

    def test_empty_token_string_reads_as_no_token(self, fake_home, monkeypatch):
        """The installer reports token_file: true on existence alone, so an empty
        token must not be treated as usable."""
        _write_token(fake_home, {"token": ""})
        monkeypatch.setenv("OPENI_TOKEN", "from-env")
        assert auth.resolve_credentials().token == "from-env"


class TestHomeIsReadAtCallTime:
    """The token path must follow the current HOME, not the one at import time.

    It used to be a module-level constant, so changing HOME (test isolation, or
    switching config in one process) still read the original file -- and which
    token file gets read is the thing that matters here.
    """

    def test_changing_home_changes_which_file_is_read(self, tmp_path, monkeypatch):
        a = tmp_path / "home_a"
        b = tmp_path / "home_b"
        for home, token in ((a, "token-a"), (b, "token-b")):
            (home / ".openi").mkdir(parents=True)
            (home / ".openi" / "token.json").write_text(
                json.dumps({"token": token}), encoding="utf-8"
            )

        monkeypatch.delenv("OPENI_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(a))
        assert auth.resolve_credentials().token == "token-a"

        monkeypatch.setenv("HOME", str(b))
        assert auth.resolve_credentials().token == "token-b"

    def test_token_file_path_is_a_function_of_home(self, fake_home):
        assert auth.token_file() == fake_home / ".openi" / "token.json"