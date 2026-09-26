"""v5.8.1 tests: direct-download cap raised; deterministic errors fail fast.

Run: python3 test_v581.py
"""
import asyncio
import io
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_download as wd

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


check("DIRECT_MAX_MB is 1900", wd.DIRECT_MAX_MB == 1900)
import inspect
sig = inspect.signature(wd.download_direct_file)
check("default max_mb is DIRECT_MAX_MB",
      sig.parameters["max_mb"].default == wd.DIRECT_MAX_MB)


class FakeHeaders(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class FakeResp:
    def __init__(self, length=None):
        self.headers = FakeHeaders(
            {"Content-Length": str(length)} if length else {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return b""


real_urlopen = urllib.request.urlopen
real_sleep = asyncio.sleep
calls = {"n": 0}


def fake_big_size(req, timeout=None):
    calls["n"] += 1
    return FakeResp(length=2000 * 1048576)  # 2000MB > 1900MB cap


async def no_sleep(d):
    pass


async def main():
    urllib.request.urlopen = fake_big_size
    asyncio.sleep = no_sleep
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            try:
                await wd.download_direct_file("http://x/y.mp4", td)
                check("oversize raises", False)
            except RuntimeError as e:
                check("oversize raises", "ကြီးလွန်းပါတယ်" in str(e))
                check("no retry on deterministic size error",
                      calls["n"] == 1)
    finally:
        urllib.request.urlopen = real_urlopen
        asyncio.sleep = real_sleep


asyncio.run(main())


# transient errors still retry 3x, message has single type prefix
async def main2():
    n = {"c": 0}

    def fake_flaky(req, timeout=None):
        n["c"] += 1
        raise ConnectionError("boom")

    urllib.request.urlopen = fake_flaky
    asyncio.sleep = no_sleep
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            try:
                await wd.download_direct_file("http://x/y.mp4", td)
                check("flaky raises", False)
            except RuntimeError as e:
                s = str(e)
                check("flaky raises", "3 ကြိမ်" in s)
                check("3 attempts", n["c"] == 3)
                check("no type name baked into message",
                      "ConnectionError" not in s and "RuntimeError" not in s)
    finally:
        urllib.request.urlopen = real_urlopen
        asyncio.sleep = real_sleep


asyncio.run(main2())

print(f"✅ v5.8.1 direct cap: {len(PASS)} tests passed")
