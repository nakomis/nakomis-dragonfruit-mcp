"""A small client for Cthulhu, Martin's print server for the Elegoo Mars 5 Ultra.

Cthulhu owns the printer connection (SDCP over the LAN) and knows the printer's
quirks, so nothing here ever talks to the printer directly. Cthulhu sits behind
Leia's nginx, which demands a client certificate (mTLS) and nothing else: there
is no API key.

Configuration, all from the environment:

- `CTHULHU_URL`: base URL, default `https://cthulhu.home.nakomis.com`.
- `CTHULHU_CLIENT_CERT` and `CTHULHU_CLIENT_KEY`: PEM paths for the mTLS client
  certificate. Required for https URLs; unused for a plain http URL (a local
  Cthulhu in front of the fake printer).
- `CTHULHU_UPLOAD_TIMEOUT_S`: how long one upload may take (default 3600). The
  printer takes about 100 KB/s over WiFi, so a 145 MB file needs about 25 minutes.

Cthulhu's REST API, as used here (apps/server/src/app.ts in the cthulhu repo):

- `GET /api/status`: the printer view (connected, machineStatus, print{...}).
- `GET /api/files`: printable files on the printer, `{files: [{path, name, ...}]}`.
- `POST /api/upload`: raw body, `x-filename` header. Answers only after the
  printer has finished checking the file and lists it in `/local`
  (`confirmUploaded()`), with `{filename, md5, size, uuid, chunks, path}`; a file
  the printer rejects (bad MD5 or format) is a 502.
- `GET /api/upload/progress`: 200 while an upload is running, 204 when none is.
- `GET /api/files/meta?path=`: layer count, layer height, estimated time (.goo).
- `POST /api/print`: `{filename: <path from upload>}`; 409 when the printer refuses.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nakomis_dragonfruit_mcp.cli import CliError

DEFAULT_URL = "https://cthulhu.home.nakomis.com"
DEFAULT_UPLOAD_TIMEOUT_S = 3600.0

# Cthulhu / SDCP machine status codes (packages/sdcp/src/protocol.ts).
MACHINE_STATUS = {
    0: "Idle",
    1: "Printing",
    2: "File transferring",
    3: "Exposure testing",
    4: "Device self-check",
}
MACHINE_IDLE = 0
MACHINE_PRINTING = 1
# Print status codes at which no print is under way (Idle, Stopped, Complete).
PRINT_NOT_ACTIVE = {0, 8, 9}


class CthulhuError(CliError):
    """Cthulhu was unconfigured, unreachable, or answered with something unexpected."""


@dataclass
class CthulhuConfig:
    url: str
    cert: str | None
    key: str | None
    upload_timeout_s: float

    @classmethod
    def from_env(cls) -> CthulhuConfig:
        url = os.environ.get("CTHULHU_URL", DEFAULT_URL).rstrip("/")
        cert = os.environ.get("CTHULHU_CLIENT_CERT") or None
        key = os.environ.get("CTHULHU_CLIENT_KEY") or None
        if url.startswith("https://"):
            if not (cert and key):
                raise CthulhuError(
                    "Cthulhu is behind Leia's mTLS and needs a client certificate: set "
                    "CTHULHU_CLIENT_CERT and CTHULHU_CLIENT_KEY to the PEM paths "
                    f"(CTHULHU_URL is {url})."
                )
            for label, path in (("CTHULHU_CLIENT_CERT", cert), ("CTHULHU_CLIENT_KEY", key)):
                if not Path(path).is_file():
                    raise CthulhuError(f"{label} points at {path}, which does not exist.")
        try:
            upload_timeout = float(
                os.environ.get("CTHULHU_UPLOAD_TIMEOUT_S", DEFAULT_UPLOAD_TIMEOUT_S)
            )
        except ValueError as e:
            raise CthulhuError("CTHULHU_UPLOAD_TIMEOUT_S must be a number of seconds") from e
        return cls(url=url, cert=cert, key=key, upload_timeout_s=upload_timeout)


class CthulhuClient:
    def __init__(
        self,
        config: CthulhuConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._sleep = sleep
        cert = (config.cert, config.key) if config.cert and config.key else None
        self._http = httpx.Client(
            base_url=config.url,
            cert=cert,
            transport=transport,
            timeout=httpx.Timeout(30.0, read=config.upload_timeout_s),
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise CthulhuError(f"Cthulhu request {method} {path} failed: {e!r}") from e

    def _json(self, response: httpx.Response, what: str) -> Any:
        if response.status_code >= 400:
            raise CthulhuError(f"Cthulhu {what}: HTTP {response.status_code}: {_reason(response)}")
        try:
            return response.json()
        except ValueError as e:
            raise CthulhuError(
                f"Cthulhu {what}: response was not JSON: {response.text[:200]!r}"
            ) from e

    def status(self) -> dict:
        """The printer view, schema-checked."""
        view = self._json(self._request("GET", "/api/status"), "status")
        if (
            not isinstance(view, dict)
            or not isinstance(view.get("print"), dict)
            or not isinstance(view.get("machineStatus"), list)
            or "connected" not in view
        ):
            raise CthulhuError(f"unexpected Cthulhu /api/status output: {str(view)[:200]!r}")
        return view

    def files(self) -> list[dict]:
        body = self._json(self._request("GET", "/api/files"), "file listing")
        if not isinstance(body, dict) or not isinstance(body.get("files"), list):
            raise CthulhuError(f"unexpected Cthulhu /api/files output: {str(body)[:200]!r}")
        return body["files"]

    def file_meta(self, path: str) -> dict | None:
        """Layer count, layer height and estimated time from the file's header, if known."""
        response = self._request("GET", "/api/files/meta", params={"path": path})
        if response.status_code == 404:
            return None
        meta = self._json(response, "file details")
        return meta if isinstance(meta, dict) else None

    def upload(self, filename: str, data: bytes) -> dict:
        """Upload and wait until the printer lists the file (MD5 verified).

        Cthulhu answers only after `confirmUploaded()`, so a 200 means the printer
        accepted the file. If the HTTP request itself dies part-way (a proxy timeout
        on a long upload), the upload may still be running or have finished: follow it
        through `/api/upload/progress` and the file listing before giving up.
        """
        path = f"/local/{filename}"
        try:
            response = self._http.post(
                "/api/upload",
                content=data,
                headers={"x-filename": filename, "content-type": "application/octet-stream"},
            )
        except httpx.HTTPError as e:
            return self._await_upload(filename, path, f"{e!r}")
        if response.status_code == 504:
            return self._await_upload(filename, path, "HTTP 504 from the proxy")
        body = self._json(response, "upload")
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            raise CthulhuError(f"unexpected Cthulhu /api/upload output: {str(body)[:200]!r}")
        return body

    def _await_upload(
        self, filename: str, path: str, why: str, *, patience_s: float = 1800
    ) -> dict:
        deadline = time.monotonic() + patience_s
        while time.monotonic() < deadline:
            progress = self._request("GET", "/api/upload/progress")
            if progress.status_code == 204:
                break
            self._sleep(5)
        else:
            raise CthulhuError(f"upload of {filename} lost ({why}) and still running after a wait")
        if any(f.get("path") == path for f in self.files()):
            return {"filename": filename, "path": path, "md5": None, "size": None}
        raise CthulhuError(
            f"upload of {filename} did not complete ({why}); the printer does not list {path}"
        )

    def start_print(self, path: str) -> None:
        response = self._request("POST", "/api/print", json={"filename": path})
        self._json(response, "start print")


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and "error" in body:
            return str(body["error"])
    except ValueError:
        pass
    return response.text[:200]


def get_client() -> CthulhuClient:
    """A client built from the environment; raises CthulhuError when unconfigured."""
    return CthulhuClient(CthulhuConfig.from_env())
