"""Deterministische Leitplanken fuer Kundentickets (kein LLM).

Laeuft VOR jeder Bewertung. Ergebnis ist ein Befund mit Urteil:
  BLOCK   — harte Grenze verletzt (Root/Privilegien, Secrets, Mandantengrenze,
            Prompt-Injection, Zerstoerung). Agenten arbeiten daran NIE; Empfehlung
            "Nicht umsetzbar" bzw. Betreiber-Entscheidung, Risiko hoch.
  REVIEW  — Architektur-/Betriebsrelevanz (neue Systeme, DNS, Zahlungen, Daten
            aller Kunden, Kapazitaet). Bewertung ja, Freigabe nur durch Betreiber.
  OK      — nichts Auffaelliges; normale Bewertung.
Ticket-Text ist Kundeneingabe: er wird geprueft, niemals als Anweisung ausgefuehrt.
"""
import re

# Harte Grenzen: Muster -> Grund. Wortgrenzen bewusst locker (deutsch/englisch, Flexion).
HART = [
    (r"\b(als|as)\s+root\b|\broot[- ]?(zugang|zugriff|access|rechte|shell|passwor)|\bsudo\b|\bsu\s+-\b", "Root/Sudo-Zugriff"),
    (r"docker\.sock|/var/run/docker|--privileged|\bprivileged\b|cap[_-]?add|host[- ]?mount|/etc/shadow|/etc/passwd", "Container-Ausbruch / Host-Zugriff"),
    (r"authorized_keys|id_(rsa|ed25519)|ssh[- ]?(key|schl[uü]ssel)|private[- ]?key|privater schl[uü]ssel", "SSH-Schluessel"),
    (r"\.env\b|secret[s]?\s*(datei|file|anzeigen|auslesen|zeigen|schicken)|api[- ]?(key|token)s?\s*(anzeigen|auslesen|zeigen|schicken|senden|geben|mir)|(passw(o|ö)rt|password|token|zugangsdaten)\w*\s*(anzeigen|auslesen|ausgeben|zeigen|schicken|senden|exportieren|mir geben)|zeig\w*\s+(mir\s+)?(das|alle|die)\s+(passw|token|secret|zugangsdaten)", "Geheimnisse auslesen"),
    (r"chmod\s+777|chown\s+-R\s+root|iptables|\bufw\b|firewall\s+(aus|deaktiv|abschalt|disable)|fail2ban\s+(aus|deaktiv|disable)", "Netz-/Rechte-Haertung aufheben"),
    (r"(sso|auth\w*|w[aä]chter|guard|2fa|zwei-faktor|single[- ]sign[- ]on)\s*\w*\s*(deaktivier|abschalt|umgeh|aushebel|bypass|disable)|(deaktivier|abschalt|umgeh|aushebel|bypass|disable)\w*\s+(?:\w+\s+){0,2}(sso|auth\w*|2fa|w[aä]chter|guard)\b|ohne\s+(passwort|anmeldung)\s+(zugriff|zugang|admin)|admin[- ]?(zugang|rechte|konto)\s+(f[uü]r\s+mich|geben|anlegen|erstellen)", "Authentifizierung umgehen"),
    (r"rm\s+-rf|drop\s+(database|table)|truncate\s+table|alle\s+(daten|backups?|kunden(daten)?)\s+(l[oö]sch|entfern|vernicht)|backups?\s+(l[oö]sch|deaktivier|abschalt)|volumes?\s+l[oö]sch", "Zerstoerende Operation"),
    (r"(curl|wget)[^\n|]*\|\s*(ba)?sh\b|base64\s+-d|eval\(|reverse[- ]?shell|nc\s+-e|/dev/tcp/|xmrig|min(er|ing)\b|krypto[- ]?min", "Fremdcode / Mining / Shell"),
    (r"(ignor|vergiss|forget|disregard|missachte)\w*\s+(?:\w+\s+){0,3}(anweisung|instruktion|instruction|regeln|rules|system\s*prompt|leitplanken|guardrails)|du\s+bist\s+jetzt|you\s+are\s+now|jailbreak|developer\s+mode|act\s+as\s+(root|admin)", "Prompt-Injection"),
    (r"(anderer|andere|fremde[rn]?)\s+(kunde|mandant|tenant|server|deployment)|kundendaten\s+(von|anderer)|alle\s+mandanten", "Mandantengrenze"),
]

# Betriebs-/Architekturrelevanz: Bewertung ja, Freigabe nur durch Betreiber.
REVIEW = [
    (r"kubernetes|\bk8s\b|openshift|nomad|\bswarm\b", "Fremde Orchestrierung (nicht im Stack)"),
    (r"neue[rn]?\s+(server|vm|maschine|instanz|host)|zweite[rn]?\s+server|hetzner\s+cloud|aws|azure|gcp|google\s+cloud", "Neue Infrastruktur"),
    (r"\bgpu\b|grafikkarte|cuda|llm\s+(lokal|selbst|hosten)|eigenes\s+modell\s+trainier|fine-?tun", "GPU/Modell-Hosting"),
    (r"windows|\.net\s+framework|active\s+directory|exchange\s+server|sharepoint", "Nicht im Linux/Docker-Stack"),
    (r"migration|migrier|umzug|umziehen|komplett\s+neu|neu\s+aufsetzen|rewrite|neuschreib|von\s+grund\s+auf|neues?\s+system|plattform\s+wechsel", "Architekturvorhaben"),
    (r"\bdns\b|nameserver|domain\s+(umziehen|wechseln|kaufen|registrier)|zertifikat|\bssl\b|\btls\b", "DNS/Zertifikate (Betreiber)"),
    (r"zahlung|payment|stripe|paypal|kreditkarte|rechnungs?stell|abrechnung|steuer", "Zahlungen/Finanzen"),
    (r"dsgvo|gdpr|personenbezogen|gesundheitsdaten|auskunft|l[oö]schkonzept|export\s+aller\s+(nutzer|kontakte|daten)", "Datenschutz-relevant"),
    (r"massen(mail|versand)|newsletter\s+an\s+alle|\d{3,}\s*(mails|e-mails|empf[aä]nger)|spam", "Massenversand / Reputation"),
    (r"[oö]e?ffentlich|ohne\s+(login|anmeldung)|(login|anmeldung|passwort(schutz)?)\s+(?:\w+\s+){0,5}(entfern|weg|abschalt|deaktivier|raus)|f[uü]r\s+alle\s+sichtbar|\bpublic\b", "Oeffentliche Route / Login entfernen (Freigabe noetig)"),
    (r"port\s+\d+\s+(freigeben|[oö]ffnen|expose)|firewall\s+regel|vpn|wireguard|tailscale", "Netzwerk/Ports"),
    (r"cron|t[aä]glich\s+automatisch|scraper|crawl|bot\b|automatisch\s+\w+\s+(kaufen|buchen|posten)", "Automatisierung mit Aussenwirkung"),
    (r"alle\s+kunden|f[uü]r\s+jeden\s+kunden|plattformweit|global", "Plattformweite Aenderung"),
]

STACK_HINWEIS = ("Stack je Deployment: Docker Compose, Caddy, Nextcloud, Mautic, Site (Python/Node), Postgres/MySQL, "
                 "Mail via mxroute/Mailcow, App-Zone fuer eigene Compose-Apps (Linter: keine Host-Ports, kein Root-Zugriff, "
                 "Speicherlimits). Nicht vorgesehen: Kubernetes, VMs, Windows, GPU, Fremd-Cloud, Host-Pakete.")


def _norm(text):
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return text


def _hits(text, regeln):
    out = []
    for muster, grund in regeln:
        for variante in (text, _norm(text)):
            m = re.search(muster, variante, re.I)
            if m:
                out.append({"grund": grund, "fund": variante[max(0, m.start() - 40):m.end() + 40].replace("\n", " ").strip()})
                break
    return out


def groesse_hinweis(text, review_hits):
    woerter = len(re.findall(r"\w+", text))
    punkte = len(re.findall(r"^\s*(?:[-*•]|\d+[.)])\s+", text, re.M))
    und = len(re.findall(r"\b(und|sowie|au(ss|ß)erdem|zus[aä]tzlich|also|and|plus)\b", text, re.I))
    score = 0
    score += 1 if woerter > 150 else 0
    score += 1 if woerter > 400 else 0
    score += 1 if punkte >= 4 else 0
    score += 1 if und >= 6 else 0
    score += min(2, len(review_hits))
    return ["S", "M", "L", "XL", "XL", "XL", "XL"][min(score, 6)], {"woerter": woerter, "aufzaehlungen": punkte, "verknuepfungen": und}


def pruefen(subject, beschreibung, projekt=None, kapazitaet=None):
    text = f"{subject}\n{beschreibung}"
    hart = _hits(text, HART)
    review = _hits(text, REVIEW)
    groesse, masse = groesse_hinweis(text, review)
    kap = []
    if kapazitaet:
        if kapazitaet.get("ram_frei_mb") is not None and kapazitaet["ram_frei_mb"] < 1500:
            kap.append(f"wenig freier RAM auf {projekt}: {kapazitaet['ram_frei_mb']} MB")
        if kapazitaet.get("disk_frei_gb") is not None and kapazitaet["disk_frei_gb"] < 15:
            kap.append(f"wenig Platte auf {projekt}: {kapazitaet['disk_frei_gb']} GB frei")
        if kapazitaet.get("load1") is not None and kapazitaet.get("cpus") and kapazitaet["load1"] > kapazitaet["cpus"] * 0.8:
            kap.append(f"hohe Last auf {projekt}: load {kapazitaet['load1']} bei {kapazitaet['cpus']} CPUs")
        if kapazitaet.get("swap_used_mb", 0) > 1024:
            kap.append(f"Swap in Benutzung auf {projekt}: {kapazitaet['swap_used_mb']} MB")
    if hart:
        urteil = "BLOCK"
    elif review or kap or groesse in ("L", "XL"):
        urteil = "REVIEW"
    else:
        urteil = "OK"
    return {
        "urteil": urteil,
        "hart": hart,
        "review": review,
        "kapazitaet_hinweise": kap,
        "groesse_hinweis": groesse,
        "masse": masse,
        "stack": STACK_HINWEIS,
        "regel": ("BLOCK: nie bearbeiten, Empfehlung 'Nicht umsetzbar' oder Betreiber-Entscheidung, Risiko hoch. "
                  "REVIEW/L/XL: bewerten, Empfehlung 'Betreiber-Entscheidung'. Freigabe (Bereit) setzt immer der Betreiber."),
    }


if __name__ == "__main__":
    import json, sys
    daten = json.load(sys.stdin)
    print(json.dumps(pruefen(daten.get("subject", ""), daten.get("beschreibung", ""), daten.get("projekt"), daten.get("kapazitaet")), ensure_ascii=False, indent=1))
