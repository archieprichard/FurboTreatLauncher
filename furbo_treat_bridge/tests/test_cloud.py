"""How Furbo's answers map to errors, against a fake HTTP session."""

from __future__ import annotations

import asyncio

import pytest

import furbo_cloud
from furbo_cloud import (
    FurboAuthError,
    FurboCloud,
    FurboCodeRequired,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    encrypt_password,
)


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeHttp:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def post(self, url, json, headers, timeout):
        self.requests.append((url, json))
        return FakeResponse(*self.answers.pop(0))


def call(http, coro_fn):
    return asyncio.run(coro_fn(FurboCloud(http)))  # type: ignore[arg-type]


def login(cloud):
    return cloud.login("a@b.c", "enc", "MOBILE", None)


def test_login_success_and_proof_kept():
    http = FakeHttp((200, {"AccountId": "a", "CognitoToken": "t", "MfaAuthCode": "p"}))
    result = call(http, login)
    assert (result.account_id, result.cognito_token, result.mfa_auth_code) == ("a", "t", "p")
    assert http.requests[0][0].endswith("/v5/account/read/login")


def test_code_required():
    http = FakeHttp((400, {"Code": 12101, "AccountId": "a", "MfaAuthCodeCandidate": "c"}))
    with pytest.raises(FurboCodeRequired) as err:
        call(http, login)
    assert err.value.candidate == "c"


def test_wrong_password_is_a_login_error():
    with pytest.raises(FurboLoginError):
        call(FakeHttp((400, {"Code": 12002})), login)


def test_outage_is_temporary_not_a_bad_password():
    with pytest.raises(FurboConnectionError):
        call(FakeHttp((503, {"Message": "down"})), login)
    with pytest.raises(FurboConnectionError):
        call(FakeHttp((502, ValueError("html"))), login)


def test_lockout_is_not_reported_as_bad_password():
    with pytest.raises(FurboError, match="too many attempts") as err:
        call(FakeHttp((400, {"Code": 80001})), login)
    assert not isinstance(err.value, FurboLoginError)


def test_rate_limit_retries_then_counts_as_temporary(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(furbo_cloud.asyncio, "sleep", no_sleep)
    limited = (400, {"Code": 80002, "TimeWait": 1})
    ok = (200, {"AccountId": "a", "CognitoToken": "t"})
    assert call(FakeHttp(limited, ok), login).cognito_token == "t"
    with pytest.raises(FurboConnectionError):
        call(FakeHttp(limited, limited, limited), login)


def test_expired_token_on_other_endpoints():
    with pytest.raises(FurboAuthError):
        call(FakeHttp((400, {"Code": 12002})), lambda c: c.get_p2p_connection("a", "t", "d"))


def test_password_is_encrypted_not_sent_plain():
    enc = encrypt_password("hunter2")
    assert "hunter2" not in enc and len(enc) > 100
