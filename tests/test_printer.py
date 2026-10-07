"""printer_status and send_to_printer against a fake Cthulhu (httpx.MockTransport).

Nothing here touches the network, Cthulhu or a printer.
"""

import json

import httpx
import pytest

from nakomis_dragonfruit_mcp import cthulhu
from nakomis_dragonfruit_mcp.cli import CliError
from nakomis_dragonfruit_mcp.tools import printer


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


class FakeCthulhu:
    """Behaves like Cthulhu's REST API; records every request."""

    def __init__(self, initial=None):
        self.view = initial or view()
        self.requests: list[tuple[str, str]] = []
        self.uploaded: dict[str, bytes] = {}
        self.upload_status = 200
        self.printing_after_start = True
        self.start_status = 200
        self.files = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        self.requests.append(route)
        if route == ("GET", "/api/status"):
            return httpx.Response(200, json=self.view)
        if route == ("GET", "/api/files"):
            return httpx.Response(200, json={"files": self.files})
        if route == ("GET", "/api/files/meta"):
            return httpx.Response(
                200, json={"layerCount": 1405, "layerHeightMm": 0.05, "printTimeS": 14400}
            )
        if route == ("GET", "/api/upload/progress"):
            return httpx.Response(204)
        if route == ("POST", "/api/upload"):
            name = request.headers["x-filename"]
            if self.upload_status != 200:
                return httpx.Response(self.upload_status, json={"error": "MD5 check failed"})
            self.uploaded[name] = request.content
            return httpx.Response(
                200, json={"filename": name, "md5": "abc", "size": 3, "path": f"/local/{name}"}
            )
        if route == ("POST", "/api/print"):
            if self.start_status != 200:
                return httpx.Response(self.start_status, json={"error": "printer is busy"})
            if self.printing_after_start:
                self.view = view(machine=(1,), status=3, label="Exposing", task="t2")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "nope"})

    def started(self) -> bool:
        return ("POST", "/api/print") in self.requests


@pytest.fixture
def fake(monkeypatch):
    fake = FakeCthulhu()
    config = cthulhu.CthulhuConfig("http://cthulhu.test", None, None, 5)

    def make():
        return cthulhu.CthulhuClient(
            config, transport=httpx.MockTransport(fake), sleep=lambda _s: None
        )

    monkeypatch.setattr(cthulhu, "get_client", make)
    monkeypatch.setattr(printer.time, "sleep", lambda _s: None)
    monkeypatch.setattr(printer, "START_OBSERVE_S", 0.05)
    return fake


@pytest.fixture
def sliced(tmp_path):
    path = tmp_path / "dragon.goo"
    path.write_bytes(b"abc")
    return str(path)


GO = printer.CONFIRM_PHRASE


# -- status parsing ------------------------------------------------------------


def test_status_idle(fake):
    status = printer.printer_status()
    assert status.idle and status.connected and status.state == "Idle"
    assert status.machine_status == ["Idle"]


def test_status_printing_parses_progress(fake):
    fake.view = view(
        machine=(1,),
        status=3,
        label="Exposing",
        task="t1",
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


# -- send_to_printer -----------------------------------------------------------


def test_upload_only_does_not_start(fake, sliced):
    result = printer.send_to_printer(sliced)
    assert fake.uploaded == {"dragon.goo": b"abc"}
    assert not result.started and not fake.started()
    assert result.uploaded_path == "/local/dragon.goo"
    assert (result.layers, result.layer_height_mm, result.estimated_time_s) == (1405, 0.05, 14400)


def test_start_with_confirm_starts_and_observes(fake, sliced):
    result = printer.send_to_printer(sliced, start=True, confirm=GO)
    assert fake.started() and result.started
    assert result.status.state == "Exposing" and result.status.task_id == "t2"


def test_start_not_observed_is_not_claimed(fake, sliced):
    fake.printing_after_start = False
    result = printer.send_to_printer(sliced, start=True, confirm=GO)
    assert fake.started() and not result.started
    assert any("no print was observed" in w for w in result.warnings)


@pytest.mark.parametrize("confirm", [None, "", "yes", "martin said go", GO + "!"])
def test_start_refused_without_exact_confirm(fake, sliced, confirm):
    with pytest.raises(printer.PrinterRefused, match="confirm"):
        printer.send_to_printer(sliced, start=True, confirm=confirm)
    assert fake.requests == [] and not fake.uploaded


def test_confirm_alone_does_not_start(fake, sliced):
    result = printer.send_to_printer(sliced, start=False, confirm=GO)
    assert not result.started and not fake.started()


def test_start_refused_when_busy(fake, sliced):
    fake.view = view(machine=(1,), status=3, label="Exposing", task="t1")
    with pytest.raises(printer.PrinterRefused, match="not idle"):
        printer.send_to_printer(sliced, start=True, confirm=GO)
    assert not fake.uploaded and not fake.started()


def test_start_refused_on_error_state(fake, sliced):
    fake.view = view(error=3)
    with pytest.raises(printer.PrinterRefused, match="error 3"):
        printer.send_to_printer(sliced, start=True, confirm=GO)
    assert not fake.uploaded and not fake.started()


def test_upload_refused_during_print(fake, sliced):
    fake.view = view(machine=(1,), status=3, label="Exposing", task="t1")
    with pytest.raises(printer.PrinterRefused, match="printing"):
        printer.send_to_printer(sliced)
    assert not fake.uploaded


def test_refused_when_disconnected(fake, sliced):
    fake.view = view(connected=False)
    with pytest.raises(printer.PrinterRefused, match="not connected"):
        printer.send_to_printer(sliced)


def test_rechecks_idle_after_a_long_upload(fake, sliced, monkeypatch):
    # Someone starts a print from the touchscreen whilst our upload runs.
    original = fake.__call__

    def busy_after_upload(request):
        response = original(request)
        if request.url.path == "/api/upload":
            fake.view = view(machine=(1,), status=3, label="Exposing", task="t9")
        return response

    monkeypatch.setattr(fake, "__call__", busy_after_upload, raising=False)
    fake_transport = httpx.MockTransport(busy_after_upload)
    config = cthulhu.CthulhuConfig("http://cthulhu.test", None, None, 5)
    monkeypatch.setattr(
        cthulhu,
        "get_client",
        lambda: cthulhu.CthulhuClient(config, transport=fake_transport, sleep=lambda _s: None),
    )
    with pytest.raises(printer.PrinterRefused, match="nothing started"):
        printer.send_to_printer(sliced, start=True, confirm=GO)
    assert not fake.started()


def test_rejected_upload_never_starts(fake, sliced):
    fake.upload_status = 502
    with pytest.raises(cthulhu.CthulhuError, match="MD5 check failed"):
        printer.send_to_printer(sliced, start=True, confirm=GO)
    assert not fake.started()


def test_printer_refusing_start_raises(fake, sliced):
    fake.start_status = 409
    with pytest.raises(cthulhu.CthulhuError, match="printer is busy"):
        printer.send_to_printer(sliced, start=True, confirm=GO)


def test_bad_files_refused_before_any_request(fake, tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"x")
    with pytest.raises(printer.PrinterRefused, match=r"\.goo and \.ctb"):
        printer.send_to_printer(str(stl))
    with pytest.raises(printer.PrinterRefused, match="does not exist"):
        printer.send_to_printer(str(tmp_path / "missing.goo"))
    assert fake.requests == []


def test_upload_survives_proxy_timeout(fake, sliced):
    fake.upload_status = 504
    fake.files = [{"path": "/local/dragon.goo"}]
    result = printer.send_to_printer(sliced)
    assert result.uploaded_path == "/local/dragon.goo"


def test_upload_lost_and_file_missing_raises(fake, sliced):
    fake.upload_status = 504
    with pytest.raises(cthulhu.CthulhuError, match="did not complete"):
        printer.send_to_printer(sliced)


# -- configuration ---------------------------------------------------------------


def test_https_without_certificate_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("CTHULHU_URL", raising=False)
    monkeypatch.delenv("CTHULHU_CLIENT_CERT", raising=False)
    monkeypatch.delenv("CTHULHU_CLIENT_KEY", raising=False)
    with pytest.raises(cthulhu.CthulhuError, match="CTHULHU_CLIENT_CERT"):
        cthulhu.CthulhuConfig.from_env()


def test_missing_certificate_file_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CTHULHU_CLIENT_CERT", str(tmp_path / "no.pem"))
    monkeypatch.setenv("CTHULHU_CLIENT_KEY", str(tmp_path / "no.key"))
    with pytest.raises(cthulhu.CthulhuError, match="does not exist"):
        cthulhu.CthulhuConfig.from_env()


def test_http_url_needs_no_certificate(monkeypatch):
    monkeypatch.setenv("CTHULHU_URL", "http://localhost:9120/")
    monkeypatch.delenv("CTHULHU_CLIENT_CERT", raising=False)
    config = cthulhu.CthulhuConfig.from_env()
    assert config.url == "http://localhost:9120" and config.cert is None


def test_unreachable_cthulhu_is_a_cthulhu_error():
    def boom(_request):
        raise httpx.ConnectError("refused")

    client = cthulhu.CthulhuClient(
        cthulhu.CthulhuConfig("http://cthulhu.test", None, None, 5),
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(cthulhu.CthulhuError, match="failed"):
        client.status()


def test_result_serialises(fake, sliced):
    json.loads(printer.send_to_printer(sliced).model_dump_json())
