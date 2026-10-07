"""printer_status, send_to_printer and start_print against a fake Cthulhu.

httpx.MockTransport throughout, and an injected clock so nothing waits in real time.
Nothing here touches the network, Cthulhu or a printer.
"""

import hashlib

import httpx
import pytest

from nakomis_dragonfruit_mcp import cthulhu
from nakomis_dragonfruit_mcp.cli import CliError
from nakomis_dragonfruit_mcp.tools import printer

DATA = b"abc"
MD5 = hashlib.md5(DATA).hexdigest()


def view(machine=(0,), status=0, label="Idle", error=0, task=None, connected=True, **extra):
    return {
        "connected": connected,
        "machineStatus": list(machine),
        "print": {
            "status": status,
            "statusLabel": label,
            "filename": "old.goo",
            "currentLayer": 0,
            "totalLayer": 0,
            "progressPercent": 0,
            "remainingMs": None,
            "totalMs": None,
            "errorNumber": error,
            "errorMessage": None if not error else "MD5 check failed",
            "taskId": task,
            **extra,
        },
    }


def busy(**kw):
    return view(machine=(1,), status=3, label="Exposing", task="t1", **kw)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


class FakeCthulhu:
    """Behaves like Cthulhu's REST API; records every request."""

    def __init__(self):
        self.view = view()
        self.requests: list[tuple[str, str]] = []
        self.files: list[str] = []  # paths on the printer
        self.upload_status = 200
        self.upload_error: Exception | None = None
        self.upload_lands = False  # on a failed upload, does the file still arrive?
        self.reported_md5: str | None = None
        self.start_status = 200
        self.start_error: Exception | None = None
        self.start_lands = True
        self.status_error_after_start: Exception | None = None
        self.started_file: str | None = None
        self.posted_names: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        self.requests.append(route)
        if route == ("GET", "/api/status"):
            if self.started_file and self.status_error_after_start:
                raise self.status_error_after_start
            return httpx.Response(200, json=self.view)
        if route == ("GET", "/api/files"):
            return httpx.Response(200, json={"files": [{"path": p} for p in self.files]})
        if route == ("GET", "/api/files/meta"):
            return httpx.Response(
                200, json={"layerCount": 1405, "layerHeightMm": 0.05, "printTimeS": 14400}
            )
        if route == ("GET", "/api/upload/progress"):
            return httpx.Response(204)
        if route == ("POST", "/api/upload"):
            name = request.headers["x-filename"]
            self.posted_names.append(name)
            if self.upload_error or self.upload_status != 200:
                if self.upload_lands:
                    self.files.append(f"/local/{name}")
                if self.upload_error:
                    raise self.upload_error
                return httpx.Response(self.upload_status, json={"error": "MD5 check failed"})
            self.files.append(f"/local/{name}")
            md5 = self.reported_md5 or hashlib.md5(request.content).hexdigest()
            return httpx.Response(
                200, json={"filename": name, "md5": md5, "size": 3, "path": f"/local/{name}"}
            )
        if route == ("POST", "/api/print"):
            name = request.read() and __import__("json").loads(request.content)["filename"]
            self.started_file = name
            if self.start_error:
                if self.start_lands:
                    self._begin(name)
                raise self.start_error
            if self.start_status != 200:
                return httpx.Response(self.start_status, json={"error": "printer is busy"})
            if self.start_lands:
                self._begin(name)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "nope"})

    def _begin(self, path):
        self.view = view(
            machine=(1,), status=3, label="Exposing", task="t2", filename=path.split("/")[-1]
        )

    def started(self) -> bool:
        return ("POST", "/api/print") in self.requests


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(monkeypatch, clock):
    fake = FakeCthulhu()
    config = cthulhu.CthulhuConfig("http://localhost:9120", None, None, 5)

    def make():
        return cthulhu.CthulhuClient(
            config, transport=httpx.MockTransport(fake), sleep=clock.sleep, clock=clock
        )

    monkeypatch.setattr(cthulhu, "get_client", make)
    monkeypatch.setattr(printer, "_VERIFIED", {})
    return fake


@pytest.fixture
def sliced(tmp_path):
    path = tmp_path / "dragon.goo"
    path.write_bytes(DATA)
    return str(path)


def send(sliced):
    return printer.send_to_printer(sliced)


def go(name="dragon.goo"):
    return printer.confirm_phrase(name)


# -- status parsing ------------------------------------------------------------


def test_status_idle(fake):
    status = printer.printer_status()
    assert status.idle and status.connected and status.state == "Idle"
    assert status.machine_status == ["Idle"]


def test_status_printing_parses_progress(fake):
    fake.view = busy(
        filename="dragon.goo",
        currentLayer=12,
        totalLayer=1405,
        progressPercent=0.85,
        remainingMs=7_200_000,
        totalMs=14_400_000,
    )
    status = printer.printer_status()
    assert not status.idle
    assert (status.layer, status.total_layers, status.file) == (12, 1405, "dragon.goo")
    assert status.remaining_s == 7200 and status.total_s == 14400
    assert status.machine_status == ["Printing"]


def test_status_error_is_not_idle(fake):
    fake.view = view(error=1)
    status = printer.printer_status()
    assert not status.idle and status.error_message == "MD5 check failed"


def test_status_disconnected_warns(fake):
    fake.view = view(connected=False)
    status = printer.printer_status()
    assert not status.idle and status.warnings


def test_status_finished_print_counts_as_idle(fake):
    fake.view = view(status=9, label="Complete", task="t0")
    assert printer.printer_status().idle


def test_status_schema_change_raises(fake):
    fake.view = {"something": "else"}
    with pytest.raises(CliError, match="unexpected Cthulhu"):
        printer.printer_status()


# -- send_to_printer: upload only ----------------------------------------------


def test_send_uploads_verifies_and_never_starts(fake, sliced):
    result = send(sliced)
    assert result.verified and result.remote_name == "dragon.goo" and result.md5 == MD5
    assert not fake.started()
    assert (result.layers, result.layer_height_mm, result.estimated_time_s) == (1405, 0.05, 14400)
    assert result.confirm_phrase == "Martin said go: dragon.goo"
    assert "1405 layers" in result.summary


def test_send_has_no_start_parameter(fake, sliced):
    with pytest.raises(TypeError):
        printer.send_to_printer(sliced, start=True)


@pytest.mark.parametrize(
    "state,match",
    [
        (busy(), "not idle"),
        (view(machine=(0,), status=6, label="Paused", task="t1"), "not idle"),
        (view(error=3), "error 3"),
        (view(connected=False), "not connected"),
    ],
)
def test_send_refused_unless_idle_connected_error_free(fake, sliced, state, match):
    fake.view = state
    with pytest.raises(printer.PrinterRefused, match=match):
        send(sliced)
    assert ("POST", "/api/upload") not in fake.requests


def test_bad_files_refused_before_any_request(fake, tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"x")
    with pytest.raises(printer.PrinterRefused, match=r"\.goo and \.ctb"):
        send(str(stl))
    with pytest.raises(printer.PrinterRefused, match="does not exist"):
        send(str(tmp_path / "missing.goo"))
    assert fake.requests == []


def test_existing_name_is_never_overwritten(fake, sliced):
    fake.files = ["/local/dragon.goo"]
    result = send(sliced)
    assert result.remote_name == f"dragon-{MD5[:6]}.goo"
    assert fake.posted_names == [result.remote_name]
    assert result.confirm_phrase == f"Martin said go: dragon-{MD5[:6]}.goo"


def test_name_is_sanitised(fake, tmp_path):
    odd = tmp_path / "dra$gon (v2)é.goo"
    odd.write_bytes(DATA)
    result = send(str(odd))
    assert result.remote_name == "dra_gon _v2__.goo"


def test_md5_mismatch_is_rejected(fake, sliced):
    fake.reported_md5 = "0" * 32
    with pytest.raises(cthulhu.CthulhuError, match="Not trusting"):
        send(sliced)
    assert not printer._VERIFIED


def test_rejected_upload_is_an_error(fake, sliced):
    fake.upload_status = 502
    with pytest.raises(cthulhu.CthulhuError, match="MD5 check failed"):
        send(sliced)


# -- upload recovery -----------------------------------------------------------


def test_recovery_accepts_new_file_but_unverified(fake, sliced):
    fake.upload_status = 504
    fake.upload_lands = True
    result = send(sliced)
    assert result.uploaded and not result.verified
    assert result.confirm_phrase is None and result.warnings
    assert not printer._VERIFIED


def test_recovery_after_dropped_connection(fake, sliced):
    fake.upload_error = httpx.ReadTimeout("slow")
    fake.upload_lands = True
    assert not send(sliced).verified


def test_recovery_with_file_absent_fails(fake, sliced):
    fake.upload_status = 504
    with pytest.raises(cthulhu.CthulhuError, match="did not complete"):
        send(sliced)


def test_recovery_never_trusts_a_preexisting_name(fake, sliced):
    # The name we pick is unique, so a stale same-name file cannot be mistaken for ours:
    # an old dragon.goo stays old, and the dropped upload is judged on its own new name.
    fake.files = ["/local/dragon.goo"]
    fake.upload_status = 504
    with pytest.raises(cthulhu.CthulhuError, match="did not complete"):
        send(sliced)


def test_nothing_sent_is_a_plain_failure_not_recovery(fake, sliced):
    fake.upload_error = httpx.ConnectError("refused")
    fake.upload_lands = True  # even if a file happened to appear, do not recover
    with pytest.raises(cthulhu.CthulhuError, match="before sending"):
        send(sliced)
    assert ("GET", "/api/upload/progress") not in fake.requests


def test_local_protocol_error_is_a_plain_failure(fake, sliced):
    fake.upload_error = httpx.LocalProtocolError("bad header")
    with pytest.raises(cthulhu.CthulhuError, match="before sending"):
        send(sliced)


def test_unverified_file_cannot_be_started(fake, sliced):
    fake.upload_status = 504
    fake.upload_lands = True
    result = send(sliced)
    with pytest.raises(printer.PrinterRefused, match="MD5-verified"):
        printer.start_print(result.remote_name, go(result.remote_name))
    assert not fake.started()


# -- start_print ---------------------------------------------------------------


def test_start_after_send_starts_and_observes(fake, sliced):
    send(sliced)
    result = printer.start_print("dragon.goo", go())
    assert fake.started() and result.started and not result.start_state_unknown
    assert result.status.state == "Exposing" and result.status.task_id == "t2"


@pytest.mark.parametrize(
    "confirm",
    [None, "", "yes", "Martin said go", "martin said go: dragon.goo", "Martin said go: x.goo"],
)
def test_start_refused_without_exact_file_bound_confirm(fake, sliced, confirm):
    send(sliced)
    with pytest.raises(printer.PrinterRefused, match="confirm"):
        printer.start_print("dragon.goo", confirm)
    assert not fake.started()


def test_start_refused_for_a_file_never_sent(fake):
    with pytest.raises(printer.PrinterRefused, match="MD5-verified"):
        printer.start_print("dragon.goo", go())
    assert fake.requests == []


@pytest.mark.parametrize("state", [busy(), view(error=3), view(connected=False)])
def test_start_refused_when_not_idle(fake, sliced, state):
    send(sliced)
    fake.view = state
    with pytest.raises(printer.PrinterRefused):
        printer.start_print("dragon.goo", go())
    assert not fake.started()


def test_start_refused_when_file_vanished(fake, sliced):
    send(sliced)
    fake.files.clear()
    with pytest.raises(printer.PrinterRefused, match="not on the printer"):
        printer.start_print("dragon.goo", go())
    assert not fake.started()


def test_start_4xx_means_not_started(fake, sliced):
    send(sliced)
    fake.start_status = 409
    with pytest.raises(cthulhu.CthulhuError, match="printer is busy"):
        printer.start_print("dragon.goo", go())


def test_start_dropped_but_printing_is_observed(fake, sliced):
    send(sliced)
    fake.start_error = httpx.ReadTimeout("slow")
    result = printer.start_print("dragon.goo", go())
    assert result.started and not result.start_state_unknown


def test_start_dropped_and_not_printing_is_loudly_unknown(fake, sliced, clock):
    send(sliced)
    fake.start_error = httpx.ReadTimeout("slow")
    fake.start_lands = False
    result = printer.start_print("dragon.goo", go())
    assert not result.started and result.start_state_unknown
    assert any("START STATE UNKNOWN" in w for w in result.warnings)
    assert clock.slept >= printer.START_OBSERVE_S


def test_start_5xx_is_unknown_not_an_exception(fake, sliced):
    send(sliced)
    fake.start_status = 502
    fake.start_lands = False
    result = printer.start_print("dragon.goo", go())
    assert result.start_state_unknown and not result.started


def test_status_failure_whilst_observing_does_not_propagate(fake, sliced):
    send(sliced)
    fake.status_error_after_start = httpx.ReadTimeout("slow")
    result = printer.start_print("dragon.goo", go())
    assert not result.started
    assert any("status check failed" in w for w in result.warnings)


def test_start_not_observed_is_not_claimed(fake, sliced):
    send(sliced)
    fake.start_lands = False
    result = printer.start_print("dragon.goo", go())
    assert fake.started() and not result.started
    assert any("No print of dragon.goo was observed" in w for w in result.warnings)


def test_observed_print_of_a_different_file_is_not_a_start(fake, sliced):
    send(sliced)
    original = fake._begin
    fake._begin = lambda path: original("/local/other.goo")
    result = printer.start_print("dragon.goo", go())
    assert not result.started
    assert any("not 'dragon.goo'" in w for w in result.warnings)


def test_suite_does_not_wait_in_real_time(fake, sliced, clock):
    send(sliced)
    fake.start_lands = False
    printer.start_print("dragon.goo", go())
    assert clock.slept >= printer.START_OBSERVE_S  # simulated, not slept


# -- configuration -----------------------------------------------------------------


def test_https_without_certificate_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("CTHULHU_URL", raising=False)
    monkeypatch.delenv("CTHULHU_CLIENT_CERT", raising=False)
    monkeypatch.delenv("CTHULHU_CLIENT_KEY", raising=False)
    with pytest.raises(cthulhu.CthulhuError, match="CTHULHU_CLIENT_CERT"):
        cthulhu.CthulhuConfig.from_env()


def test_missing_certificate_file_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CTHULHU_URL", raising=False)
    monkeypatch.setenv("CTHULHU_CLIENT_CERT", str(tmp_path / "no.pem"))
    monkeypatch.setenv("CTHULHU_CLIENT_KEY", str(tmp_path / "no.key"))
    with pytest.raises(cthulhu.CthulhuError, match="does not exist"):
        cthulhu.CthulhuConfig.from_env()


@pytest.mark.parametrize("url", ["http://localhost:9120/", "http://127.0.0.1:9120"])
def test_http_loopback_needs_no_certificate(monkeypatch, url):
    monkeypatch.setenv("CTHULHU_URL", url)
    monkeypatch.delenv("CTHULHU_CLIENT_CERT", raising=False)
    config = cthulhu.CthulhuConfig.from_env()
    assert config.url == url.rstrip("/") and config.cert is None


def test_plain_http_to_a_remote_host_is_refused(monkeypatch):
    monkeypatch.setenv("CTHULHU_URL", "http://cthulhu.home.nakomis.com")
    with pytest.raises(cthulhu.CthulhuError, match="plain http"):
        cthulhu.CthulhuConfig.from_env()


def test_bad_url_and_bad_timeout(monkeypatch):
    monkeypatch.setenv("CTHULHU_URL", "ftp://x")
    with pytest.raises(cthulhu.CthulhuError, match="http"):
        cthulhu.CthulhuConfig.from_env()
    monkeypatch.setenv("CTHULHU_URL", "http://localhost")
    monkeypatch.setenv("CTHULHU_UPLOAD_TIMEOUT_S", "soon")
    with pytest.raises(cthulhu.CthulhuError, match="seconds"):
        cthulhu.CthulhuConfig.from_env()


def test_only_the_upload_gets_the_long_timeout(fake, sliced, monkeypatch):
    seen = {}
    real = httpx.Client.post

    def spy(self, url, **kw):
        seen[url] = kw.get("timeout")
        return real(self, url, **kw)

    monkeypatch.setattr(httpx.Client, "post", spy)
    send(sliced)
    assert seen["/api/upload"].read == 5  # the configured upload timeout
    config = cthulhu.CthulhuConfig("http://localhost", None, None, 5000)
    client = cthulhu.CthulhuClient(config)
    assert client._http.timeout == httpx.Timeout(cthulhu.REQUEST_TIMEOUT_S)
    assert cthulhu.REQUEST_TIMEOUT_S == 20


def test_unreachable_cthulhu_is_a_cthulhu_error():
    def boom(_request):
        raise httpx.ConnectError("refused")

    client = cthulhu.CthulhuClient(
        cthulhu.CthulhuConfig("http://localhost", None, None, 5),
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(cthulhu.CthulhuError, match="failed"):
        client.status()
