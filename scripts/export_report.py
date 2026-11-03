#!/usr/bin/env python3
"""Stream a genuine authenticated LOCAL report without printing login credentials."""

import argparse
import hashlib
import http.cookiejar
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
BASE = (
    "http://127.0.0.1:3100"  # Local credentials are never sent to a remote URL/proxy.
)
MAX_BYTES = 257 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=UUID)
    parser.add_argument(
        "--classification", choices=("VALIDATED", "RESEARCH"), default="VALIDATED"
    )
    parser.add_argument("--start", help="ISO8601 UTC inclusive cutoff")
    parser.add_argument("--end", help="ISO8601 UTC exclusive cutoff")
    parser.add_argument("--strategy", type=UUID)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )

    def request(path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        return opener.open(
            urllib.request.Request(
                BASE + "/api/" + path,
                data=body,
                headers={"Content-Type": "application/json", "Origin": BASE},
            ),
            timeout=120,
        )

    try:
        password = (ROOT / ".secrets/admin-password").read_text().strip()
        with request("auth/login", {"username": "quantix", "password": password}):
            pass
        del password
        with request("paper/sessions") as response:
            sessions = json.load(response)["items"]
        session = (
            next((s for s in sessions if str(args.session) == s["id"]), None)
            if args.session
            else next(
                (s for s in sessions if s["classification"] == args.classification),
                None,
            )
        )
        if session is None:
            raise ValueError("Requested owned paper portfolio not found")
        params = {"session_id": session["id"]}
        for key, value in (
            ("start", args.start),
            ("end", args.end),
            ("strategy_version_id", args.strategy),
        ):
            if value:
                params[key] = str(value)
        target = (
            args.output
            or ROOT
            / "reports"
            / f"quantix-analysis-{session['id']}-{uuid4().hex[:8]}.zip"
        )
        if target.exists():
            raise ValueError("Output already exists; choose another path")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(
            prefix=".quantix-export-", dir=target.parent
        )
        try:
            size = 0
            digest = hashlib.sha256()
            with (
                os.fdopen(descriptor, "wb") as output,
                request("reports/export?" + urllib.parse.urlencode(params)) as response,
            ):
                if response.headers.get_content_type() != "application/zip":
                    raise ValueError("Server did not return a ZIP archive")
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Archive exceeds the local size bound")
                    digest.update(chunk)
                    output.write(chunk)
                expected = response.headers.get("X-Content-SHA256")
                if expected and digest.hexdigest() != expected:
                    raise ValueError("Archive checksum mismatch")
            # Exclusive link avoids replacing a file created while download was running.
            os.link(filename, target)
            print(f"Saved {target} ({size} bytes, {session['classification']})")
            print("SHA256 " + digest.hexdigest())
        finally:
            Path(filename).unlink(missing_ok=True)
    except urllib.error.HTTPError as error:
        detail = "Report request failed"
        try:
            value = json.loads(error.read(4096))
            if isinstance(value.get("detail"), str):
                detail = value["detail"]
        except (ValueError, OSError):
            pass
        raise SystemExit(f"HTTP {error.code}: {detail}") from None
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    finally:
        try:
            with request("auth/logout", {}):
                pass
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
