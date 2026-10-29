"""The bridge's sign-in flow, treat limits and camera-link handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import bridge as bridge_mod
from bridge import CODE_REQUIRED, ERROR, READY, Bridge, BridgeError, Options, StateStore
from furbo_cloud import (
    FurboAuthError,
    FurboCodeError,
    FurboCodeRequired,
    FurboConnectionError,
    FurboLoginError,
    LoginResult,
)
from furbo_p2p import P2PCredentials, P2PError

DEVICE = {
    "Id": "dev1",
    "DeviceName": "Living room",
    "ProductId": "FB0030",
    "P2PUuid": "UIDUIDUIDUIDUIDUIDU1",
    "SecretField": "not kept",
}
P2P = {"AuthKey": "abcdefgh", "P2PAccountId": "acct", "P2PAccountKey": "key"}


class FakeCloud:
    def __init__(self, *, needs_code: bool = False) -> None:
        self.needs_code = needs_code
        self.calls: list[tuple[str, Any]] = []
        self.p2p_errors: list[Exception] = []
        self.login_errors: list[Exception] = []
        self.devices = [DEVICE]
        self.valid_codes = {"123456"}

    async def login(self, email, enc, mobile_id, proof):
        self.calls.append(("login", proof))
        if self.login_errors:
            raise self.login_errors.pop(0)
        if email != "me@example.com":
            raise FurboLoginError("bad")
        if self.needs_code and proof != "PROOF":
            raise FurboCodeRequired("acct", "cand-1")
        return LoginResult("acct", "token-1", proof)

    async def send_code(self, candidate):
        self.calls.append(("send_code", candidate))
        return "cand-2"

    async def verify_code(self, email, enc, mobile_id, candidate, code):
        self.calls.append(("verify_code", (candidate, code)))
        if code not in self.valid_codes:
            raise FurboCodeError("wrong")
        return LoginResult("acct", "token-2", "PROOF")

    async def get_devices(self, account_id, token):
        self.calls.append(("get_devices", token))
        return self.devices

    async def get_p2p_connection(self, account_id, token, device_id):
        self.calls.append(("p2p", token))
        if self.p2p_errors:
            raise self.p2p_errors.pop(0)
        return P2P


class FakeLink:
    instances: list[FakeLink] = []

    def __init__(self, creds: P2PCredentials) -> None:
        self.creds = creds
        self.is_open = False
        self.commands: list[str] = []
        self.fail_next = False
        FakeLink.instances.append(self)

    def open(self) -> None:
        self.is_open = True

    def command(self, name: str) -> str:
        if self.fail_next:
            raise P2PError("camera went away")
        self.commands.append(name)
        return "acknowledged"

    def close(self) -> None:
        self.is_open = False


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset_links():
    FakeLink.instances.clear()


def make(tmp_path: Path, cloud: FakeCloud, **opts: Any) -> tuple[Bridge, Clock]:
    clock = Clock()
    options = Options(email=opts.pop("email", "me@example.com"), password="pw", **opts)
    b = Bridge(
        options,
        StateStore(tmp_path / "state.json"),
        http=None,  # type: ignore[arg-type]
        cloud=cloud,  # type: ignore[arg-type]
        link_factory=FakeLink,
        clock=clock,
    )
    return b, clock


def run(coro):
    return asyncio.run(coro)


def test_sign_in_without_code_stores_session_and_trims_device(tmp_path):
    b, _ = make(tmp_path, FakeCloud())
    assert run(b.sign_in()) == READY
    stored = StateStore(tmp_path / "state.json")
    assert stored.get("cognito_token") == "token-1"
    assert stored.get("devices") == [{k: DEVICE[k] for k in ("Id", "DeviceName", "ProductId", "P2PUuid")}]
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    assert b.status()["cameras"][0]["name"] == "Living room"


def test_code_flow_then_proof_skips_code_next_time(tmp_path):
    cloud = FakeCloud(needs_code=True)
    b, _ = make(tmp_path, cloud)
    assert run(b.sign_in()) == CODE_REQUIRED
    assert ("send_code", "cand-1") in cloud.calls

    assert run(b.submit_code(" 123456 ")) == READY
    assert ("verify_code", ("cand-2", "123456")) in cloud.calls
    assert b.store.get("mfa_auth_code") == "PROOF"
    assert b.store.get("pending_candidate") is None

    # A later sign-in presents the proof and is not asked for a code.
    cloud.calls.clear()
    assert run(b.sign_in()) == READY
    assert ("login", "PROOF") in cloud.calls
    assert not any(c[0] == "send_code" for c in cloud.calls)


def test_wrong_code_is_reported_and_login_stays_parked(tmp_path):
    b, _ = make(tmp_path, FakeCloud(needs_code=True))
    run(b.sign_in())
    with pytest.raises(BridgeError) as err:
        run(b.submit_code("999999"))
    assert err.value.kind == "bad_code"
    assert b.login_state == CODE_REQUIRED
    with pytest.raises(BridgeError) as err:
        run(b.submit_code("abc"))
    assert err.value.kind == "bad_request"


def test_code_without_pending_login_conflicts(tmp_path):
    b, _ = make(tmp_path, FakeCloud())
    run(b.sign_in())
    with pytest.raises(BridgeError) as err:
        run(b.submit_code("123456"))
    assert err.value.kind == "conflict"


def test_bad_password_and_missing_credentials(tmp_path):
    b, _ = make(tmp_path, FakeCloud(), email="wrong@example.com")
    assert run(b.sign_in()) == ERROR
    assert "refused" in b.login_error
    other = tmp_path / "other"
    other.mkdir()
    b2, _ = make(other, FakeCloud(), email="")
    assert run(b2.sign_in()) == ERROR
    assert "email and password" in b2.login_error


def test_toss_reuses_link_and_counts(tmp_path):
    b, clock = make(tmp_path, FakeCloud(), min_interval=5)
    run(b.sign_in())
    result = run(b.toss())
    assert result["results"] == ["acknowledged"]
    assert result["treats_today"] == 1
    clock.now += 10
    run(b.toss())
    assert len(FakeLink.instances) == 1
    assert FakeLink.instances[0].commands == ["toss", "toss"]
    assert b.treats_today() == 2
    assert b.store.get("last_toss")
    b.close()
    assert not FakeLink.instances[0].is_open


def test_min_interval_blocks_rapid_requests(tmp_path):
    b, clock = make(tmp_path, FakeCloud(), min_interval=5)
    run(b.sign_in())
    run(b.toss())
    clock.now += 2
    with pytest.raises(BridgeError) as err:
        run(b.toss())
    assert err.value.kind == "rate_limited"
    assert err.value.extra["retry_after"] == 3.0
    b.close()


def test_daily_limit_counts_whole_burst(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mod, "BURST_SPACING", 0)
    b, clock = make(tmp_path, FakeCloud(), min_interval=0, daily_limit=3)
    run(b.sign_in())
    run(b.toss(count=2))
    assert b.treats_today() == 2
    with pytest.raises(BridgeError) as err:
        run(b.toss(count=2))
    assert err.value.kind == "rate_limited"
    run(b.toss(count=1))
    assert b.treats_today() == 3
    with pytest.raises(BridgeError):
        run(b.toss(count=6))
    b.close()


def test_expired_token_signs_in_again(tmp_path):
    cloud = FakeCloud()
    b, _ = make(tmp_path, cloud)
    run(b.sign_in())
    cloud.p2p_errors = [FurboAuthError("dead", 12002)]
    run(b.toss())
    logins = [c for c in cloud.calls if c[0] == "login"]
    assert len(logins) == 2
    b.close()


def test_expired_token_needing_code_tells_the_caller(tmp_path):
    cloud = FakeCloud()
    b, _ = make(tmp_path, cloud)
    run(b.sign_in())
    cloud.needs_code = True
    b.store.update(mfa_auth_code=None)
    cloud.p2p_errors = [FurboAuthError("dead", 12002)]
    with pytest.raises(BridgeError) as err:
        run(b.toss())
    assert err.value.kind == "code_required"
    assert b.login_state == CODE_REQUIRED


def test_waiting_for_code_does_not_email_another_on_each_toss(tmp_path):
    cloud = FakeCloud()
    b, _ = make(tmp_path, cloud, min_interval=0)
    run(b.sign_in())
    # The session expires and Furbo now wants a code.
    cloud.needs_code = True
    b.store.update(mfa_auth_code=None)
    cloud.p2p_errors = [FurboAuthError("dead", 12002)]
    for _ in range(3):
        with pytest.raises(BridgeError) as err:
            run(b.toss())
        assert err.value.kind == "code_required"
    assert [c for c in cloud.calls if c[0] == "send_code"] == [("send_code", "cand-1")]


def test_camera_failure_on_a_fresh_link_is_reported(tmp_path, monkeypatch):
    b, _ = make(tmp_path, FakeCloud(), min_interval=0)
    run(b.sign_in())
    run(b.toss())
    FakeLink.instances[0].fail_next = True

    def failing_open(self):
        raise P2PError("camera offline")

    # The reused link fails, and so does the one fresh reconnect.
    monkeypatch.setattr(FakeLink, "open", failing_open)
    with pytest.raises(BridgeError) as err:
        run(b.toss())
    assert err.value.kind == "camera"
    assert not FakeLink.instances[0].is_open
    assert len(FakeLink.instances) == 2  # exactly one reconnect attempt
    assert b.treats_today() == 1
    b.close()


def test_camera_selection(tmp_path):
    b, _ = make(tmp_path, FakeCloud())
    run(b.sign_in())
    with pytest.raises(BridgeError) as err:
        run(b.toss(device_id="nope"))
    assert err.value.kind == "not_found"
    assert run(b.treat_sound(device_id="dev1"))["results"] == ["acknowledged"]
    b.close()


# --- regressions from the bug check ------------------------------------------


def test_restart_while_waiting_for_code_keeps_the_emailed_one(tmp_path):
    cloud = FakeCloud(needs_code=True)
    b, _ = make(tmp_path, cloud)
    run(b.start())
    assert b.login_state == CODE_REQUIRED
    # The add-on restarts before the code is entered.
    cloud2 = FakeCloud(needs_code=True)
    b2 = Bridge(
        b.options, StateStore(tmp_path / "state.json"), http=None, cloud=cloud2, link_factory=FakeLink
    )
    run(b2.start())
    assert b2.login_state == CODE_REQUIRED
    assert cloud2.calls == []  # no second email
    assert run(b2.submit_code("123456")) == READY


def test_unreachable_furbo_at_boot_retries_until_signed_in(tmp_path, monkeypatch):
    cloud = FakeCloud()
    cloud.login_errors = [FurboConnectionError("dns"), FurboConnectionError("dns")]
    b, _ = make(tmp_path, cloud)
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", fast_sleep)

    async def scenario():
        await b.start()
        assert b.login_state == ERROR
        await b._retry_task

    run(scenario())
    assert b.login_state == READY
    assert sleeps == [30.0, 60.0]


def test_wrong_password_is_not_retried_in_the_background(tmp_path):
    b, _ = make(tmp_path, FakeCloud(), email="wrong@example.com")

    async def scenario():
        await b.start()
        return b._retry_task

    assert run(scenario()) is None
    assert b.login_state == ERROR


def test_interval_only_counts_treats_that_went_out(tmp_path, monkeypatch):
    b, _ = make(tmp_path, FakeCloud(), min_interval=5)
    run(b.sign_in())

    def failing_open(self):
        raise P2PError("offline")

    with monkeypatch.context() as patch:
        patch.setattr(FakeLink, "open", failing_open)
        with pytest.raises(BridgeError) as err:
            run(b.toss())
        assert err.value.kind == "camera"
    # Straight away, no 5-second wait: nothing was dispensed.
    assert run(b.toss())["results"] == ["acknowledged"]
    b.close()


def test_interval_is_per_camera(tmp_path):
    cloud = FakeCloud()
    cloud.devices = [DEVICE, {**DEVICE, "Id": "dev2", "DeviceName": "Kitchen"}]
    b, _ = make(tmp_path, cloud, min_interval=5)
    run(b.sign_in())
    run(b.toss("dev1"))
    run(b.toss("dev2"))  # a different camera is not held off
    with pytest.raises(BridgeError):
        run(b.toss("dev1"))
    b.close()


def test_dead_reused_link_reconnects_once(tmp_path):
    b, clock = make(tmp_path, FakeCloud(), min_interval=0)
    run(b.sign_in())
    run(b.toss())
    FakeLink.instances[0].fail_next = True  # the camera dropped the link silently
    assert run(b.toss())["results"] == ["acknowledged"]
    assert len(FakeLink.instances) == 2
    assert b.treats_today() == 2
    b.close()


def test_toss_during_startup_sign_in_does_not_sign_in_twice(tmp_path):
    cloud = FakeCloud(needs_code=True)
    b, _ = make(tmp_path, cloud)
    # A camera is already known from an earlier session whose token was cleared.
    b.store.update(devices=[{k: DEVICE[k] for k in ("Id", "DeviceName", "ProductId", "P2PUuid")}])
    b._sync_cameras()

    async def scenario():
        start = asyncio.create_task(b.start())
        await asyncio.sleep(0)  # startup sign-in is now underway
        with pytest.raises(BridgeError) as err:
            await b.toss()
        await start
        return err.value.kind

    assert run(scenario()) == "code_required"
    assert [c[0] for c in cloud.calls].count("send_code") == 1
    assert [c[0] for c in cloud.calls].count("login") == 1


def test_successful_sign_in_clears_a_stale_pending_code(tmp_path):
    cloud = FakeCloud(needs_code=True)
    b, _ = make(tmp_path, cloud)
    run(b.sign_in())
    assert b.store.get("pending_candidate")
    cloud.needs_code = False  # e.g. the account turned off two-step verification
    run(b.sign_in(force_new_code=True))
    assert b.login_state == READY
    assert b.store.get("pending_candidate") is None
