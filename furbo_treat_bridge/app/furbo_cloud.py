"""Minimal async client for the Furbo cloud API.

Only what a treat toss needs: sign in (with Furbo's emailed verification code),
list the cameras on the account, and fetch the short-lived P2P credentials used
to open the encrypted link to a camera.

Endpoints, headers and the password encryption were reverse engineered by the
ha-furbo project (https://github.com/willbeeching/ha-furbo, MIT licensed) and
verified there against a Furbo 360 (FB0030). This file is a reduced
reimplementation of that client.

Response bodies can contain tokens, so they never appear in exceptions or logs:
errors carry only the endpoint, HTTP status and Furbo's numeric result code.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
from typing import Any
import uuid

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://product.furbo.co"
# The app's fixed client credentials, sent on every request.
BASIC_AUTH = "Basic dG9tb2Z1bnJkOmhhcHB5UGV0MTIz"
USER_AGENT = "Furbo/7.65.0 (Linux; Android 14)"
TIMEOUT = aiohttp.ClientTimeout(total=20)

# Public key embedded in the Furbo app, used to encrypt the password at login.
RSA_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDwg8rbwkNIBmtGaS9GBURspabtzLdKCl24iZL9FFLsxexYqm2rb79wmVHptJg5JdrxufmIx9aZgnE/I9CiI7EWHzAKdupsya6LEQUg++rY18T8zvVaZnLzAqZC4+93SX5BGljDQ/khc0KH/ZvaJBDuHZGtI4J08wIK/8lLmDZ6lwIDAQAB
-----END PUBLIC KEY-----"""

CODE_TOKEN_INVALID = 12002  # also "wrong email or password" on the login endpoint
CODE_MFA_REQUIRED = 12101
CODE_TOO_MANY_ATTEMPTS = 80001
CODE_RATE_LIMITED = 80002  # body carries TimeWait in seconds

# Furbo rate limits repeat calls; never sleep less than this or more than the cap.
_RATE_LIMIT_MIN_WAIT = 10.0
_RATE_LIMIT_MAX_WAIT = 120.0


class FurboError(Exception):
    """A Furbo API failure. The message never contains a response body."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class FurboConnectionError(FurboError):
    """Furbo could not be reached, or answered with something unreadable."""


class FurboAuthError(FurboError):
    """The session token was rejected; a new sign-in is needed."""


class FurboLoginError(FurboError):
    """Furbo refused the email and password."""


class FurboCodeError(FurboError):
    """The emailed verification code was wrong or has expired."""


class FurboCodeRequired(FurboError):
    """Signing in needs an emailed verification code."""

    def __init__(self, account_id: str, candidate: str) -> None:
        super().__init__("Furbo wants an emailed verification code", CODE_MFA_REQUIRED)
        self.account_id = account_id
        self.candidate = candidate


@dataclass
class LoginResult:
    """What a successful sign-in hands back."""

    account_id: str
    cognito_token: str
    # Proof that this client has passed a verification. Presented on later
    # sign-ins so Furbo does not email a new code every time.
    mfa_auth_code: str | None


def encrypt_password(password: str) -> str:
    """RSA-encrypt the password the way the Furbo app does."""
    key = serialization.load_pem_public_key(RSA_PUBLIC_KEY)
    encrypted = key.encrypt(password.encode(), padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(encrypted).decode()


def new_mobile_id() -> str:
    """A client identity. Kept and reused: a new one looks like a new phone."""
    return str(uuid.uuid4()).upper()


def _required_str(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise FurboConnectionError(f"Malformed response from {path}: missing {key}")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _wait_seconds(data: dict[str, Any]) -> float:
    try:
        wait = float(data.get("TimeWait"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        wait = _RATE_LIMIT_MIN_WAIT
    return min(max(wait, _RATE_LIMIT_MIN_WAIT), _RATE_LIMIT_MAX_WAIT)


class FurboCloud:
    """Furbo cloud API client."""

    def __init__(self, http: aiohttp.ClientSession, base_url: str = BASE_URL) -> None:
        self._http = http
        self._base_url = base_url

    async def _post(self, path: str, payload: dict[str, Any], retry: int = 2) -> dict[str, Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": BASIC_AUTH,
            "Content-Type": "application/json",
        }
        try:
            async with self._http.post(
                f"{self._base_url}{path}", json=payload, headers=headers, timeout=TIMEOUT
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError as err:
                    raise FurboConnectionError(
                        f"Malformed response from {path}: status {status}, not JSON"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FurboConnectionError(f"Could not reach Furbo on {path}: {type(err).__name__}") from err

        if not isinstance(data, dict):
            raise FurboConnectionError(f"Malformed response from {path}: not an object")
        if status == 200:
            return data

        code = data.get("Code") if isinstance(data.get("Code"), int) else None
        if code == CODE_RATE_LIMITED and retry > 0:
            wait = _wait_seconds(data)
            _LOGGER.warning("Furbo rate-limited %s; retrying in %.0fs", path, wait)
            await asyncio.sleep(wait)
            return await self._post(path, payload, retry - 1)
        if code == CODE_MFA_REQUIRED:
            raise FurboCodeRequired(
                _required_str(data, "AccountId", path),
                _required_str(data, "MfaAuthCodeCandidate", path),
            )
        message = f"Furbo API error {status} (code {code}) on {path}"
        if code == CODE_TOKEN_INVALID:
            raise FurboAuthError(message, code)
        if status >= 500 or code == CODE_RATE_LIMITED:
            # Furbo is down or still throttling: temporary, worth retrying later.
            raise FurboConnectionError(message, code)
        if code == CODE_TOO_MANY_ATTEMPTS:
            raise FurboError(
                f"Furbo has locked sign-in after too many attempts; wait a while ({message})", code
            )
        raise FurboError(message, code)

    # --- sign-in -------------------------------------------------------------

    async def login(
        self, email: str, enc_password: str, mobile_id: str, mfa_auth_code: str | None
    ) -> LoginResult:
        """Sign in. Raises FurboCodeRequired when Furbo wants an emailed code."""
        path = "/v5/account/read/login"
        payload = {"Email": email, "EncPassword": enc_password, "MobileId": mobile_id}
        if mfa_auth_code:
            payload["MfaAuthCode"] = mfa_auth_code
        try:
            data = await self._post(path, payload)
        except FurboAuthError as err:
            # The login endpoint answers a bad email or password with 12002.
            raise FurboLoginError(str(err), err.code) from err
        return self._login_result(data, path, mfa_auth_code)

    async def send_code(self, candidate: str) -> str:
        """Ask Furbo to email a verification code. Returns the candidate to verify."""
        path = "/v4/account/mfa/login/send-code"
        data = await self._post(path, {"MfaAuthCodeCandidate": candidate, "Model": "Android"})
        return _optional_str(data, "MfaAuthCodeCandidate") or candidate

    async def verify_code(
        self, email: str, enc_password: str, mobile_id: str, candidate: str, code: str
    ) -> LoginResult:
        """Check the emailed code and finish signing in."""
        verify_path = "/v4/account/mfa/verify"
        try:
            verify = await self._post(verify_path, {"MfaAuthCodeCandidate": candidate, "Code": code})
        except FurboConnectionError:
            raise
        except FurboError as err:
            raise FurboCodeError(str(err), err.code) from err
        proof = _required_str(verify, "MfaAuthCode", verify_path)
        login_path = "/v2/account/login"
        data = await self._post(
            login_path,
            {
                "Email": email,
                "EncPassword": enc_password,
                "MobileId": mobile_id,
                "MfaAuthCode": proof,
            },
        )
        return self._login_result(data, login_path, proof)

    @staticmethod
    def _login_result(data: dict[str, Any], path: str, fallback_proof: str | None) -> LoginResult:
        return LoginResult(
            account_id=_required_str(data, "AccountId", path),
            cognito_token=_required_str(data, "CognitoToken", path),
            mfa_auth_code=_optional_str(data, "MfaAuthCode") or fallback_proof,
        )

    # --- cameras ---------------------------------------------------------------

    async def get_devices(self, account_id: str, token: str) -> list[dict[str, Any]]:
        """The cameras bound to the account."""
        path = f"/v2/account/{account_id}/device"
        data = await self._post(path, {"CognitoToken": token})
        devices = data.get("DeviceList", [])
        if not isinstance(devices, list) or not all(isinstance(d, dict) for d in devices):
            raise FurboConnectionError(f"Malformed response from {path}: DeviceList")
        return [d for d in devices if d.get("Id") is not None]

    async def get_p2p_connection(self, account_id: str, token: str, device_id: str) -> dict[str, Any]:
        """AuthKey, P2PAccountId and a freshly issued P2PAccountKey for one camera.

        Furbo reissues P2PAccountKey on every call, so this is fetched just
        before each connection.
        """
        path = "/v5/device/p2p_connection/get"
        return await self._post(path, {"AccountId": account_id, "CognitoToken": token, "DeviceId": device_id})
