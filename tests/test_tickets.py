"""Tickets (Fork v0.10): das Gateway reicht Rolle + Absender-Adresse an die
Kontrollebene weiter und validiert die Eingaben, bevor ein Request entsteht."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.tools import tickets


def _aufzeichner(antwort=None):
    calls = []

    def call(method, path="", body=None, params=None):
        calls.append({"method": method, "path": path, "body": body, "params": params})
        return antwort if antwort is not None else {"ok": True, "id": 42, "url": "https://tickets.example/t/42"}

    return calls, call


def test_configured_braucht_alle_drei(monkeypatch):
    for k in ("TASK_QUEUE_TICKETS_ENABLED", "TASK_QUEUE_CONTROL_URL", "TASK_QUEUE_CONTROL_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert tickets.configured() is False
    monkeypatch.setenv("TASK_QUEUE_TICKETS_ENABLED", "1")
    monkeypatch.setenv("TASK_QUEUE_CONTROL_URL", "http://web:3000")
    assert tickets.configured() is False
    monkeypatch.setenv("TASK_QUEUE_CONTROL_SECRET", "s3cret")
    assert tickets.configured() is True


def test_control_base_leitet_pfad_aus_basis_url_ab(monkeypatch):
    monkeypatch.setenv("TASK_QUEUE_CONTROL_URL", "http://web:3000/")
    monkeypatch.setenv("TASK_QUEUE_CONTROL_BASE_URL", "https://apps.example.com/agents/")
    assert tickets.control_base() == "http://web:3000/agents/internal/tickets"
    monkeypatch.delenv("TASK_QUEUE_CONTROL_BASE_URL")
    assert tickets.control_base() == "http://web:3000/internal/tickets"


def test_create_reicht_rolle_und_ip_durch():
    calls, call = _aufzeichner()
    out = tickets.ticket_create_handler(
        actor="crm-manager", titel="Segment-Export scheitert am Mautic-Token",
        beschreibung="segment_preview antwortet 401; Token laut doctor abgelaufen, Fix liegt in der .env.",
        art="Bug", bereich="agenten", quelle="doctor", ip="10.42.0.7", call=call,
    )
    assert out["ok"] is True and out["id"] == 42
    assert len(calls) == 1
    b = calls[0]["body"]
    assert calls[0]["method"] == "POST" and calls[0]["path"] == ""
    assert b["actor"] == "crm-manager" and b["ip"] == "10.42.0.7"
    assert b["art"] == "fehler"            # "Bug" wird normalisiert
    assert b["bereich"] == "Agenten"       # Gross-/Kleinschreibung tolerant
    assert b["transkript"] is True and b["quelle"] == "doctor"


@pytest.mark.parametrize("kwargs, fragment", [
    ({"titel": "", "beschreibung": "x" * 60}, "titel"),
    ({"titel": "T", "beschreibung": "zu kurz"}, "zu kurz"),
    ({"titel": "T", "beschreibung": "x" * 60, "art": "story"}, "art"),
    ({"titel": "T", "beschreibung": "x" * 60, "bereich": "Weltall"}, "bereich"),
])
def test_create_validiert_vor_dem_request(kwargs, fragment):
    calls, call = _aufzeichner()
    out = tickets.ticket_create_handler(actor="qa-tester", call=call, **kwargs)
    assert out["ok"] is False and fragment in out["error"]
    assert calls == []


def test_list_get_comment():
    calls, call = _aufzeichner({"ok": True})
    tickets.ticket_list_handler(status="Neu", limit=500, call=call)
    tickets.ticket_get_handler(ticket_id="#17", call=call)
    tickets.ticket_comment_handler(actor="support-ops", ticket_id=17, text=" neuer Stand ", call=call)
    assert calls[0] == {"method": "GET", "path": "", "body": None, "params": {"status": "Neu", "limit": 100}}
    assert calls[1]["path"] == "/17"
    assert calls[2] == {"method": "POST", "path": "/17/comment",
                        "body": {"actor": "support-ops", "text": "neuer Stand"}, "params": None}
    assert tickets.ticket_get_handler(ticket_id="abc", call=call)["ok"] is False
    assert tickets.ticket_comment_handler(actor="x", ticket_id=1, text="  ", call=call)["ok"] is False


class _Kontrollebene(BaseHTTPRequestHandler):
    gesehen = []

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        laenge = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(laenge) or b"{}")
        self.gesehen.append((self.path, self.headers.get("x-dispatch-secret"), body))
        if self.headers.get("x-dispatch-secret") != "geheim":
            return self._send(401, {"ok": False, "error": "unauthorized"})
        if self.path.endswith("/aus/internal/tickets"):
            return self._send(404, {"ok": False, "error": "nicht eingerichtet"})
        if self.path.endswith("/999/comment"):
            return self._send(404, {"ok": False, "error": "Ticket #999 gibt es in diesem Projekt nicht."})
        return self._send(200, {"ok": True, "id": 7, "url": "https://tickets.example/t/7"})

    def log_message(self, *a):  # still
        pass


@pytest.fixture
def kontrollebene():
    srv = HTTPServer(("127.0.0.1", 0), _Kontrollebene)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_call_gegen_kontrollebene(monkeypatch, kontrollebene):
    monkeypatch.setenv("TASK_QUEUE_CONTROL_URL", kontrollebene)
    monkeypatch.setenv("TASK_QUEUE_CONTROL_BASE_URL", "https://apps.example/agents")
    monkeypatch.setenv("TASK_QUEUE_CONTROL_SECRET", "geheim")
    out = tickets.ticket_create_handler(actor="qa-tester", titel="T", beschreibung="x" * 60, ip="10.0.0.9")
    assert out == {"ok": True, "id": 7, "url": "https://tickets.example/t/7"}
    pfad, secret, body = _Kontrollebene.gesehen[-1]
    assert pfad == "/agents/internal/tickets" and secret == "geheim" and body["ip"] == "10.0.0.9"
    # 404 der Kontrollebene = nicht eingerichtet, in Worten
    monkeypatch.setenv("TASK_QUEUE_CONTROL_BASE_URL", "https://apps.example/aus")
    out = tickets.ticket_create_handler(actor="qa-tester", titel="T", beschreibung="x" * 60)
    assert out["ok"] is False and "nicht eingerichtet" in out["error"]
    # 404 MIT Text = normale Antwort (fremde/unbekannte Nummer), nicht "nicht eingerichtet"
    out = tickets.ticket_comment_handler(actor="qa-tester", ticket_id=999, text="hallo")
    assert out["ok"] is False and "gibt es in diesem Projekt nicht" in out["error"]
    # falsches Secret
    monkeypatch.setenv("TASK_QUEUE_CONTROL_BASE_URL", "https://apps.example/agents")
    monkeypatch.setenv("TASK_QUEUE_CONTROL_SECRET", "falsch")
    out = tickets.ticket_create_handler(actor="qa-tester", titel="T", beschreibung="x" * 60)
    assert out["ok"] is False and "unauthorized" in out["error"]


def test_peer_ip_ausserhalb_eines_requests_ist_none():
    assert tickets.peer_ip() is None
