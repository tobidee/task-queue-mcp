"""
Tickets: eine Rolle, die am Ende ihrer Mittel ist, legt ein Ticket beim Kunden an.

Warum hier: das Queue-Gateway ist das eine Werkzeug, das JEDE Rolle hat, und sein
Bearer-Token sagt zweifelsfrei, welche Rolle spricht (auth.py). Angelegt wird das
Ticket aber nicht von hier aus, sondern von der Kontrollebene (agents-web,
app/src/tickets.js): nur dort liegen der Docker-Socket — fuer das Transkript der
Sitzung aus der Sandbox — und die Zugangsdaten des Ticketsystems. Dieser Modul
reicht darum genau zwei Dinge weiter, die die Kontrollebene selbst nicht wissen
kann: die authentifizierte Rolle und die Absender-Adresse des MCP-Aufrufs. Aus
der Adresse findet die Kontrollebene die Sandbox (Container-Labels), aus der
Sandbox die laufende Sitzung. Nichts davon ist eine Behauptung des Aufrufers.

Konfiguration (docker-compose.yml des agents-Stacks):

    TASK_QUEUE_TICKETS_ENABLED=1          nur dann werden die Werkzeuge registriert
    TASK_QUEUE_CONTROL_URL=http://<agents-web>:3000
    TASK_QUEUE_CONTROL_BASE_URL=https://apps.<domain>/agents   (Basis-Pfad daraus)
    TASK_QUEUE_CONTROL_SECRET=<AGENTS_INTERNAL_SECRET>

Fehlt eines, gibt es die Werkzeuge nicht — kein halb funktionierendes Ticket-
System, das "nicht eingerichtet" antwortet, wenn eine Rolle gerade festhaengt.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90  # Transkript lesen + Anhang hochladen dauert bei langen Sitzungen

ARTEN = ("aufgabe", "fehler", "feature")
BEREICHE = (
    "Apps", "Frontend", "Backend", "Cloud/Infrastruktur", "Agenten", "Prozesse",
    "Daten/Integrationen", "Mail/Kommunikation", "Sicherheit/Zugaenge", "Sonstiges",
)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def configured() -> bool:
    return (
        _env("TASK_QUEUE_TICKETS_ENABLED") == "1"
        and bool(_env("TASK_QUEUE_CONTROL_URL"))
        and bool(_env("TASK_QUEUE_CONTROL_SECRET"))
    )


def control_base() -> str:
    """Basis der internen Routen der Kontrollebene: Host aus CONTROL_URL, Pfad aus
    der oeffentlichen Basis-URL (dieselbe Ableitung wie app/src/config.js)."""
    host = _env("TASK_QUEUE_CONTROL_URL").rstrip("/")
    pfad = ""
    base = _env("TASK_QUEUE_CONTROL_BASE_URL")
    if base:
        pfad = urllib.parse.urlparse(base).path.rstrip("/")
    return f"{host}{pfad}/internal/tickets"


def peer_ip() -> str | None:
    """Absender-Adresse des laufenden MCP-Aufrufs — None ausserhalb eines Requests."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception:  # noqa: BLE001 — stdio, Tests
        return None
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or None


def _call(method: str, path: str = "", body: dict | None = None, params: dict | None = None) -> dict:
    url = control_base() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-dispatch-secret", _env("TASK_QUEUE_CONTROL_SECRET"))
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            antwort = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            antwort = {}
        fehler = antwort.get("error") or f"Kontrollebene antwortet HTTP {e.code}"
        # 404 ohne erklaerenden Text (oder "nicht eingerichtet") = die Funktion
        # existiert nicht; ein 404 MIT Text ("Ticket #7 gibt es ... nicht") ist
        # eine normale Antwort und bleibt so stehen.
        if e.code == 404 and (not antwort.get("error") or antwort.get("error") == "nicht eingerichtet"):
            fehler = (
                "Ticketsystem ist in dieser Installation nicht eingerichtet "
                "(AGENTS_TICKETS_URL/TOKEN/PROJECT an der Kontrollebene)."
            )
        return {"ok": False, "error": fehler}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Kontrollebene nicht erreichbar ({url}): {e.reason}"}
    except TimeoutError:
        return {"ok": False, "error": "Kontrollebene antwortet nicht (Zeitueberschreitung)."}


def ticket_create_handler(
    *,
    actor: str,
    titel: str,
    beschreibung: str,
    art: str = "aufgabe",
    bereich: str = "Agenten",
    quelle: str = "",
    mit_transkript: bool = True,
    ip: str | None = None,
    call=_call,
) -> dict:
    titel = (titel or "").strip()
    beschreibung = (beschreibung or "").strip()
    if not titel:
        return {"ok": False, "error": "titel fehlt — ein Satz, der das Problem benennt."}
    if len(beschreibung) < 40:
        return {
            "ok": False,
            "error": "beschreibung ist zu kurz — Befund (Werkzeug, Argumente, woertliche "
                     "Antwort, Vermutung) und was der Betreiber tun muesste. Das Transkript "
                     "haengt die Kontrollebene an, die Beschreibung muss trotzdem fuer sich stehen.",
        }
    art_n = (art or "aufgabe").strip().lower()
    if art_n == "bug":
        art_n = "fehler"
    if art_n not in ARTEN:
        return {"ok": False, "error": f"art muss eines von {', '.join(ARTEN)} sein (nicht {art!r})."}
    bereich_n = (bereich or "Agenten").strip()
    passend = next((b for b in BEREICHE if b.lower() == bereich_n.lower()), None)
    if passend is None:
        return {"ok": False, "error": f"bereich muss eines von {', '.join(BEREICHE)} sein (nicht {bereich!r})."}
    ergebnis = call("POST", "", body={
        "actor": actor,
        "ip": ip or "",
        "titel": titel[:255],
        "beschreibung": beschreibung[:20000],
        "art": art_n,
        "bereich": passend,
        "quelle": (quelle or "")[:200],
        "transkript": bool(mit_transkript),
    })
    if ergebnis.get("ok"):
        logger.info("ticket.create actor=%s id=%s ip=%s", actor, ergebnis.get("id"), ip or "-")
    return ergebnis


def ticket_list_handler(*, status: str | None = None, limit: int = 20, call=_call) -> dict:
    return call("GET", "", params={"status": status or "", "limit": max(1, min(100, int(limit or 20)))})


def ticket_get_handler(*, ticket_id, call=_call) -> dict:
    try:
        n = int(str(ticket_id).lstrip("#"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "ticket_id muss eine Nummer sein."}
    return call("GET", f"/{n}")


def ticket_comment_handler(*, actor: str, ticket_id, text: str, call=_call) -> dict:
    try:
        n = int(str(ticket_id).lstrip("#"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "ticket_id muss eine Nummer sein."}
    if not (text or "").strip():
        return {"ok": False, "error": "text fehlt."}
    return call("POST", f"/{n}/comment", body={"actor": actor, "text": text.strip()[:20000]})
