"""Test setup: put the add-on's app/ on the path.

The bridge's logic is tested with a fake Furbo cloud and a fake camera link, so
no network is used. When aiohttp is not installed (it is in the add-on image),
a minimal stand-in is registered so the modules import.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

try:
    import aiohttp  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    stub = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ClientSession:
        pass

    stub.ClientError = ClientError  # type: ignore[attr-defined]
    stub.ClientSession = ClientSession  # type: ignore[attr-defined]
    stub.ClientTimeout = lambda **kw: kw  # type: ignore[attr-defined]
    sys.modules["aiohttp"] = stub
