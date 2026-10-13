import requests
import hashlib
import time
import json
import os
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ============================================================
# KONFIGURATION
# ============================================================

BASE_URL = "http://192.168.10.1"

# Router credentials stay in a local, ignored file or environment variable.
CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "cudy_credentials.json"
)


def lade_router_passwort():
    passwort = os.environ.get("CUDY_ROUTER_PASSWORD")

    if passwort:
        return passwort

    try:
        with open(
            CREDENTIALS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            daten = json.load(f)

        if isinstance(daten, dict):
            passwort = daten.get("router_password", "")
            return passwort if isinstance(passwort, str) else ""

    except (OSError, json.JSONDecodeError):
        return ""

    return ""


PASSWORT = lade_router_passwort()

SMS_STATE_FILE = "sms_state.json"

# Polling
POLL_INTERVAL = 10

# Regelmäßiger Session-Refresh
SESSION_REFRESH_INTERVAL = 5 * 60

# Netzwerk-Timeout
REQUEST_TIMEOUT = 10

# Watchdog
MAX_POLL_FEHLER = 3
WATCHDOG_TIMEOUT = 45

# Lidl Connect
LIDL_ABSENDER = "80808"

# Refill
REFILL_TRIGGERS = [
    "fast verbraucht",
    "antworte einfach auf diese sms mit refill",
    "option verbraucht",
    "option ist jetzt verbraucht",
    "option ist verbraucht",
    "option wurde verbraucht",
    "option aufgebraucht",
]

REFILL_TEXT = "Refill"
REFILL_SEND_LOCK = threading.Lock()

# ------------------------------------------------------------
# SMS CLEANUP
# ------------------------------------------------------------

INBOX_CLEANUP_START = 100
INBOX_CLEANUP_TARGET = 80

OUTBOX_CLEANUP_START = 50
OUTBOX_CLEANUP_TARGET = 40

DELETE_WAIT = 1


# ============================================================
# GUI / THREADING
# ============================================================

log_queue = queue.Queue()

programm_laeuft = False
backend_thread = None

status_data = {
    "status": "Gestoppt",
    "router": "Nicht verbunden",
    "inbox": "-",
    "letzter_refill": "-",
    "cleanup": "-",
    "watchdog": "Aktiv",
    "letzter_poll": "-"
}


class QueueLogger:
    """
    Leitet print()-Ausgaben gleichzeitig in die Konsole
    und in die GUI weiter.
    """

    def __init__(self, original_stdout):
        self.original_stdout = original_stdout

    def write(self, text):
        if text:
            self.original_stdout.write(text)
            self.original_stdout.flush()

            log_queue.put(
                ("log", text)
            )

    def flush(self):
        self.original_stdout.flush()


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


# ============================================================
# STATUS
# ============================================================

letzter_erfolgreicher_poll = time.monotonic()
letzter_session_refresh = time.monotonic()
poll_fehler = 0


# ============================================================
# GUI STATUS HELFER
# ============================================================

def gui_status(name, value):
    status_data[name] = value

    log_queue.put(
        ("status", name, value)
    )


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def sha256(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def extrahiere_feld(html, name):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    feld = soup.find(
        "input",
        {"name": name}
    )

    if feld:
        return feld.get(
            "value",
            ""
        )

    return ""


# ============================================================
# LOGIN
# EXAKT AUS DER FUNKTIONIERENDEN V3.2
# ============================================================

def hole_token():
    response = session.post(
        f"{BASE_URL}/cgi-bin/luci/admin/get_token",
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    token = response.text.strip()

    print(
        "🔑 Token erhalten:",
        token[:8] + "..."
    )

    return token


def login():
    print()
    print("🔐 Starte Cudy Login...")

    if not PASSWORT:
        print("❌ Router-Passwort fehlt. Lege es in cudy_credentials.json oder CUDY_ROUTER_PASSWORD ab.")
        gui_status("router", "Passwort fehlt")
        return False
    gui_status(
        "router",
        "Verbinde..."
    )

    try:
        response = session.get(
            f"{BASE_URL}/cgi-bin/luci/",
            timeout=REQUEST_TIMEOUT
        )

        html = response.text

        csrf = extrahiere_feld(
            html,
            "_csrf"
        )

        salt = extrahiere_feld(
            html,
            "salt"
        )

        username = extrahiere_feld(
            html,
            "luci_username"
        )

        if not username:
            username = "admin"

        token = hole_token()

        pass_hash = sha256(
            PASSWORT + salt
        )

        pass_hash = sha256(
            pass_hash + token
        )

        daten = {
            "_csrf": csrf,
            "token": token,
            "salt": salt,
            "zonename": "Europe/Berlin",
            "timeclock": str(
                int(time.time())
            ),
            "luci_language": "auto",
            "luci_username": username,
            "luci_password": pass_hash,
        }

        login_response = session.post(
            f"{BASE_URL}/cgi-bin/luci/",
            data=daten,
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT
        )

        if login_response.status_code == 302:
            print(
                "🟢 LOGIN ERFOLGREICH!"
            )

            gui_status(
                "router",
                "Verbunden"
            )

            gui_status(
                "status",
                "Aktiv"
            )

            return True

        print(
            "🔴 LOGIN FEHLGESCHLAGEN:",
            login_response.status_code
        )

        gui_status(
            "router",
            "Login fehlgeschlagen"
        )

        return False

    except requests.exceptions.RequestException as e:
        print(
            "🔴 LOGIN-NETZWERKFEHLER:",
            e
        )

        gui_status(
            "router",
            "Netzwerkfehler"
        )

        return False

    except Exception as e:
        print(
            "🔴 LOGIN-FEHLER:",
            e
        )

        gui_status(
            "router",
            "Fehler"
        )

        return False


# ============================================================
# SMS-LISTE – EINE SEITE
# ============================================================

def hole_sms_seite(
    smsbox="rec",
    page=1
):
    url = (
        f"{BASE_URL}"
        "/cgi-bin/luci/admin/network/gcom/sms/"
        "smslist"
        f"?smsbox={smsbox}"
        f"&iface=4g"
        f"&pagestart=1"
        f"&pagecurrent={page}"
    )

    headers = {
        **HEADERS,
        "Referer": (
            f"{BASE_URL}"
            "/cgi-bin/luci/admin/network/gcom/sms/"
            f"smslist?smsbox={smsbox}&iface=4g"
        )
    }

    response = session.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )

    if response.status_code == 403:
        raise PermissionError(
            "Cudy Session abgelaufen."
        )

    response.raise_for_status()

    return response.text


# ============================================================
# ANZAHL DER SEITEN ERMITTELN
# ============================================================

def ermittle_letzte_seite(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    letzte_seite = 1

    for element in soup.find_all(
        attrs={"onclick": True}
    ):
        onclick = element.get(
            "onclick",
            ""
        )

        if "pagecurrent=" not in onclick:
            continue

        teile = onclick.split(
            "pagecurrent="
        )

        if len(teile) < 2:
            continue

        rest = teile[1]

        zahl = ""

        for zeichen in rest:
            if zeichen.isdigit():
                zahl += zeichen
            else:
                break

        if zahl:
            try:
                seite = int(zahl)

                if seite > letzte_seite:
                    letzte_seite = seite

            except ValueError:
                pass

    return letzte_seite


# ============================================================
# ALLE SMS-SEITEN HOLEN
# ============================================================

def hole_alle_sms_seiten(
    smsbox="rec"
):
    erste_seite = hole_sms_seite(
        smsbox,
        1
    )

    letzte_seite = ermittle_letzte_seite(
        erste_seite
    )

    print(
        f"   📄 {smsbox.upper()}: "
        f"{letzte_seite} Seite(n)"
    )

    seiten = {
        1: erste_seite
    }

    for page in range(
        2,
        letzte_seite + 1
    ):
        try:
            seiten[page] = hole_sms_seite(
                smsbox,
                page
            )

        except PermissionError:
            raise

        except Exception as e:
            print(
                f"⚠️ Seite {page} konnte "
                f"nicht geladen werden:",
                e
            )

    return seiten


# ============================================================
# SMS AUS HTML PARSEN
# ============================================================

def lese_sms(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    sms_liste = []

    rows = soup.find_all("tr")

    bereits_gefunden = set()

    for row in rows:

        phone_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-phone"
            )
        )

        content_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-content"
            )
        )

        timestamp_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-timestamp"
            )
        )

        if not content_element:
            continue

        if not timestamp_element:
            continue

        phone = phone_element.find("p") if phone_element else None
        content = content_element.find("p")
        timestamp = timestamp_element.find("p")

        if not content:
            continue

        if not timestamp:
            continue

        sms = {
            "phone":
                phone.get_text(strip=True) if phone else "",

            "content":
                content.get_text(
                    strip=True
                ),

            "timestamp":
                timestamp.get_text(
                    strip=True
                )
        }

        identifikation = sms_id(
            sms
        )

        if identifikation in bereits_gefunden:
            continue

        bereits_gefunden.add(
            identifikation
        )

        sms_liste.append(
            sms
        )

    return sms_liste


# ============================================================
# SMS MIT CFG PARSEN
# ============================================================

def lese_sms_mit_cfg(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    sms_liste = []

    bereits_gefunden = set()

    rows = soup.find_all("tr")

    for row in rows:

        phone_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-phone"
            )
        )

        content_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-content"
            )
        )

        timestamp_element = row.find(
            id=lambda value:
            value and value.endswith(
                "-timestamp"
            )
        )

        if not content_element:
            continue

        if not timestamp_element:
            continue

        phone = phone_element.find("p") if phone_element else None
        content = content_element.find("p")
        timestamp = timestamp_element.find("p")

        if not content:
            continue

        if not timestamp:
            continue

        phone_text = phone.get_text(strip=True) if phone else ""

        content_text = content.get_text(
            strip=True
        )

        timestamp_text = timestamp.get_text(
            strip=True
        )

        sms = {
            "phone":
                phone_text,

            "content":
                content_text,

            "timestamp":
                timestamp_text
        }

        identifikation = sms_id(
            sms
        )

        if identifikation in bereits_gefunden:
            continue

        button = row.find(
            "button",
            {
                "name":
                lambda value:
                value and value.endswith(
                    ".readsms"
                )
            }
        )

        if not button:
            continue

        onclick = button.get(
            "onclick",
            ""
        )

        cfg = extrahiere_cfg(
            onclick
        )

        if not cfg:
            continue

        sms["cfg"] = cfg

        bereits_gefunden.add(
            identifikation
        )

        sms_liste.append(
            sms
        )

    return sms_liste


# ============================================================
# CFG AUS ONCLICK
# ============================================================

def extrahiere_cfg(onclick):
    marker = "cfg="

    if marker not in onclick:
        return None

    cfg_start = (
        onclick.find(marker)
        + len(marker)
    )

    rest = onclick[
        cfg_start:
    ]

    cfg = ""

    for zeichen in rest:

        if zeichen in (
            "&",
            "'",
            '"',
            ")",
            " "
        ):
            break

        cfg += zeichen

    if not cfg:
        return None

    return cfg


# ============================================================
# SMS-ID
# ============================================================

def sms_id(sms):
    text = (
        sms["phone"]
        + "|"
        + sms["content"]
        + "|"
        + sms["timestamp"]
    )

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


# ============================================================
# STATUSDATEI
# ============================================================

def lade_sms_state():

    if not os.path.exists(
        SMS_STATE_FILE
    ):
        return {}

    try:
        with open(
            SMS_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            daten = json.load(
                f
            )

        if isinstance(
            daten,
            dict
        ):
            return daten

        return {}

    except Exception as e:

        print(
            "⚠️ sms_state.json konnte "
            "nicht gelesen werden:",
            e
        )

        return {}


def speichere_sms_state(state):

    temp_file = (
        SMS_STATE_FILE
        + ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        SMS_STATE_FILE
    )


# ============================================================
# OUTBOX SNAPSHOT
# ============================================================

def outbox_snapshot():

    seiten = hole_alle_sms_seiten(
        "sto"
    )

    snapshot = set()

    for html in seiten.values():

        sms_liste = lese_sms(
            html
        )

        for sms in sms_liste:

            if sms["phone"] != LIDL_ABSENDER:
                continue

            if (
                sms["content"]
                .strip()
                .lower()
                != REFILL_TEXT.lower()
            ):
                continue

            snapshot.add(
                sms_id(sms)
            )

    return snapshot


# ============================================================
# SMS REPLY
# ============================================================

def sende_reply(
    cfg,
    antwort
):
    print()
    print(
        "📤 Starte SMS-Reply..."
    )

    print(
        "   Nachricht:",
        antwort
    )

    details_url = (
        f"{BASE_URL}"
        "/cgi-bin/luci/admin/network/gcom/sms/"
        f"readsms?iface=4g&cfg={cfg}&smsbox=rec"
    )

    headers = {
        **HEADERS,
        "Referer": (
            f"{BASE_URL}"
            "/cgi-bin/luci/admin/network/gcom/sms/"
            f"smslist?smsbox=rec&iface=4g"
        )
    }

    response = session.get(
        details_url,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )

    if response.status_code == 403:
        raise PermissionError(
            "Cudy Session beim Reply abgelaufen."
        )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    form = soup.find(
        "form",
        {"name": "cbi"}
    )

    if not form:
        print(
            "❌ Reply-Formular nicht gefunden."
        )
        return False

    action = form.get(
        "action"
    )

    if not action:
        print(
            "❌ Formular-Action nicht gefunden."
        )
        return False

    if action.startswith("/"):
        action_url = (
            BASE_URL
            + action.replace(
                "&#38;",
                "&"
            )
        )

    else:
        action_url = action

    daten = {}

    for feld in form.find_all(
        "input"
    ):

        name = feld.get(
            "name"
        )

        if not name:
            continue

        daten[name] = feld.get(
            "value",
            ""
        )

    phone_field = form.find(
        "input",
        {
            "name":
            "cbid.smsread.1.phone"
        }
    )

    empfaenger = phone_field.get("value", "").strip() if phone_field else ""
    if empfaenger != LIDL_ABSENDER:
        print("❌ Reply abgebrochen: Absendernummer im Detailformular ist nicht eindeutig 80808.")
        return False

    daten[
        "cbid.smsread.1.phone"
    ] = empfaenger

    daten[
        "cbid.smsread.1.content"
    ] = antwort

    daten[
        "cbid.smsread.1.reply"
    ] = "Reply"

    daten[
        "cbi.submit"
    ] = "1"

    daten[
        "timeclock"
    ] = str(
        int(time.time())
    )

    print(
        "📡 Sende Reply an Cudy..."
    )

    send_response = session.post(
        action_url,
        data=daten,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )

    if send_response.status_code == 403:
        raise PermissionError(
            "Cudy Session beim Senden abgelaufen."
        )

    print(
        "   HTTP:",
        send_response.status_code
    )

    print(
        "   Antwort-Länge:",
        len(send_response.text)
    )

    if send_response.status_code not in (
        200,
        302
    ):

        print(
            "❌ Cudy hat den Reply "
            "nicht akzeptiert."
        )

        return False

    print()

    print(
        "🟢 REPLY ERFOLGREICH AN CUDY ÜBERMITTELT!"
    )

    return True


# ============================================================
# EINEN REFILL-VERSUCH
# ============================================================

def refill_versuch(
    cfg,
    outbox_vorher
):
    print()

    print(
        "🔄 Starte EINEN Refill-Versuch..."
    )

    with REFILL_SEND_LOCK:
        erfolg = sende_reply(
            cfg,
            REFILL_TEXT
        )

    if not erfolg:
        return False

    print(
        "⏳ Warte auf Cudy-Verarbeitung..."
    )

    time.sleep(3)

    print(
        "🔎 Prüfe Outbox auf NEUE Refill-SMS..."
    )

    try:

        outbox_nachher = outbox_snapshot()

    except Exception as e:

        print(
            "⚠️ Outbox konnte nach dem "
            "Senden nicht geprüft werden:",
            e
        )

        return False

    neue_refills = (
        outbox_nachher
        - outbox_vorher
    )

    if neue_refills:

        print()

        print(
            "✅ NEUES REFILL IN OUTBOX BESTÄTIGT!"
        )

        gui_status(
            "letzter_refill",
            time.strftime("%H:%M:%S")
        )

        return True

    print()

    print(
        "⚠️ Kein neues Refill in der Outbox gefunden."
    )

    return False


def router_session_sicherstellen():
    """Reuse the current login; renew it only when the router rejects it."""
    sms_page_url = f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms?iface=4g"
    response = session.get(sms_page_url, timeout=REQUEST_TIMEOUT)
    login_form = BeautifulSoup(response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    )

    if response.status_code == 403 or login_form:
        print("🔐 Router-Session abgelaufen; erneuere Login einmalig.")
        if not login():
            print("❌ Manueller Refill fehlgeschlagen: Router-Login fehlgeschlagen.")
            return False
        response = session.get(sms_page_url, timeout=REQUEST_TIMEOUT)
        login_form = BeautifulSoup(response.text, "html.parser").find(
            "input", {"name": "luci_password"}
        )

    if response.status_code == 403 or login_form:
        print("❌ Manueller Refill fehlgeschlagen: Router-Session nicht verfügbar.")
        return False

    response.raise_for_status()
    return True


def sende_neue_sms(empfaenger, nachricht):
    """Send a new SMS from the Cudy SMS page supplied by the router UI."""
    sms_page_url = (
        f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms?iface=4g"
    )
    response = session.get(sms_page_url, timeout=REQUEST_TIMEOUT)
    login_form = BeautifulSoup(response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    )
    if response.status_code == 403 or login_form:
        raise PermissionError("Router-Session ist vor dem SMS-Versand abgelaufen.")

    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    form_response = response
    form_soup = soup
    form = soup.find("form", {"name": "cbi"})

    # Einige Firmware-Versionen zeigen das Compose-Formular direkt auf /sms,
    # andere verlinken von dort auf eine separate New-Message-Seite.
    if not form:
        new_message_link = None
        for link in soup.find_all("a", href=True):
            link_text = link.get_text(" ", strip=True).lower()
            if "new message" in link_text or "neue nachricht" in link_text:
                new_message_link = urljoin(response.url, link["href"])
                break

        if not new_message_link:
            print("❌ SMS-Seite geöffnet, aber kein New-Message-Formular oder Link gefunden.")
            return False

        form_response = session.get(new_message_link, timeout=REQUEST_TIMEOUT)
        if form_response.status_code == 403:
            raise PermissionError("Router-Session ist vor dem SMS-Versand abgelaufen.")
        form_response.raise_for_status()
        form_soup = BeautifulSoup(form_response.text, "html.parser")
        form = form_soup.find("form", {"name": "cbi"})
    if not form:
        print("❌ Formular für neue SMS wurde nicht gefunden.")
        return False

    fields = {}
    for field in form.find_all(["input", "textarea", "select"]):
        name = field.get("name")
        if not name:
            continue
        field_type = (field.get("type") or "").lower()
        if field_type in ("submit", "button", "reset", "file"):
            continue
        if field.name == "select":
            selected = field.find("option", selected=True) or field.find("option")
            fields[name] = selected.get("value", "") if selected else ""
        else:
            fields[name] = field.get("value", "") if field.name == "input" else field.get_text()

    phone_names = [name for name in fields if any(token in name.lower() for token in ("phone", "number", "recipient", "receiver", ".to"))]
    text_names = [name for name in fields if any(token in name.lower() for token in ("content", "message", "text", ".msg"))]
    if len(phone_names) != 1 or len(text_names) != 1 or phone_names[0] == text_names[0]:
        print("❌ Empfänger- und Textfelder im SMS-Formular sind nicht eindeutig erkennbar.")
        return False

    fields[phone_names[0]] = empfaenger
    fields[text_names[0]] = nachricht
    if "cbi.submit" in {item.get("name") for item in form.find_all("input")}:
        fields["cbi.submit"] = "1"

    action = form.get("action")
    if not action:
        print("❌ Formularziel für neue SMS fehlt.")
        return False
    action_url = urljoin(form_response.url, action)
    headers = {**HEADERS, "Referer": form_response.url}
    result = session.post(action_url, data=fields, headers=headers, timeout=REQUEST_TIMEOUT)
    if result.status_code == 403:
        raise PermissionError("Cudy Session beim manuellen SMS-Versand abgelaufen.")
    if result.status_code not in (200, 302):
        print("❌ Cudy hat die neue SMS nicht angenommen:", result.status_code)
        return False
    print("📤 SMS wurde an den Router übermittelt.")
    return True


# ============================================================
# SMS STATE
# ============================================================

def aktualisiere_sms_state(
    sms,
    state
):
    identifikation = sms_id(
        sms
    )

    if identifikation not in state:

        state[identifikation] = {
            "phone":
                sms["phone"],

            "content":
                sms["content"],

            "timestamp":
                sms["timestamp"],

            "read_count":
                0,

            "refill_attempts":
                0,

            "refill_success":
                False,

            "last_attempt":
                0,

            "created":
                time.time()
        }

    eintrag = state[
        identifikation
    ]

    eintrag[
        "read_count"
    ] += 1

    return identifikation, eintrag


# ============================================================
# SMS VERARBEITEN
# ============================================================

def verarbeite_sms_liste(
    sms_liste,
    html
):
    state = lade_sms_state()

    print()

    print(
        "🧠 Gespeicherte SMS-States:",
        len(state)
    )

    geschützte_sms = set()

    for sms in sms_liste:

        identifikation, eintrag = (
            aktualisiere_sms_state(
                sms,
                state
            )
        )

        if sms["phone"] and sms["phone"] != LIDL_ABSENDER:
            continue

        # ====================================================
        # NEUER REFILL-TRIGGER-POOL
        # ====================================================

        sms_text = sms["content"].lower()

        trigger_gefunden = any(
            trigger in sms_text
            for trigger in REFILL_TRIGGERS
        )

        if not trigger_gefunden:
            continue

        geschützte_sms.add(
            identifikation
        )

        print()

        print(
            "🚨 REFILL-SMS ERKANNT!"
        )

        print(
            "   Absender:",
            sms["phone"]
        )

        print(
            "   Text:",
            sms["content"]
        )

        print(
            "   Zeit:",
            sms["timestamp"]
        )

        print(
            "   👀 Read Count:",
            eintrag["read_count"]
        )

        print(
            "   🔄 Refill Attempts:",
            eintrag["refill_attempts"]
        )

        print(
            "   ✅ Refill Success:",
            eintrag["refill_success"]
        )

        if eintrag["refill_success"]:

            print(
                "   ⏭️ Refill bereits bestätigt."
            )

            continue

        if eintrag["read_count"] > 2:

            print(
                "   ⚠️ SMS wurde bereits "
                "mehrfach geprüft."
            )

            if eintrag["refill_attempts"] >= 2:

                print(
                    "   ⏭️ Maximale Refill-Versuche erreicht."
                )

            continue

        if eintrag["refill_attempts"] >= 2:

            print(
                "   ⚠️ Bereits 2 Refill-Versuche."
            )

            continue

        cfg = finde_cfg_fuer_sms(
            html,
            sms
        )

        if not cfg:

            print()

            print(
                "❌ CFG NICHT GEFUNDEN!"
            )

            print(
                "   SMS bleibt offen "
                "und wird erneut geprüft."
            )

            continue

        print(
            "   🔎 SMS cfg:",
            cfg
        )

        try:

            outbox_vorher = outbox_snapshot()

        except Exception as e:

            print()

            print(
                "⚠️ Outbox-Snapshot fehlgeschlagen:",
                e
            )

            continue

        eintrag[
            "refill_attempts"
        ] += 1

        eintrag[
            "last_attempt"
        ] = time.time()

        speichere_sms_state(
            state
        )

        try:

            erfolg = refill_versuch(
                cfg,
                outbox_vorher
            )

        except PermissionError:

            print()

            print(
                "🔐 Session während Refill abgelaufen."
            )

            eintrag[
                "refill_attempts"
            ] -= 1

            speichere_sms_state(
                state
            )

            raise

        except Exception as e:

            print()

            print(
                "❌ FEHLER BEIM REFILL:",
                e
            )

            erfolg = False

        if erfolg:

            eintrag[
                "refill_success"
            ] = True

            speichere_sms_state(
                state
            )

            print()

            print(
                "🎉🎉🎉 REFILL ERFOLGREICH! 🎉🎉🎉"
            )

        else:

            print()

            print(
                "❌ REFILL NICHT BESTÄTIGT."
            )

            if (
                eintrag["read_count"] < 2
                and eintrag["refill_attempts"] < 2
            ):

                print(
                    "   🔁 Bei der nächsten "
                    "Prüfung erfolgt Versuch #2."
                )

            else:

                print(
                    "   ⚠️ Keine weiteren Versuche "
                    "für diese SMS."
                )

            speichere_sms_state(
                state
            )

    speichere_sms_state(
        state
    )

    return geschützte_sms


# ============================================================
# CFG EINER SMS FINDEN
# ============================================================

def finde_cfg_fuer_sms(
    html,
    sms
):
    sms_liste = lese_sms_mit_cfg(
        html
    )

    passende_sms = []

    for gespeicherte_sms in sms_liste:

        if sms["phone"] and gespeicherte_sms["phone"] != sms["phone"]:
            continue

        if (
            gespeicherte_sms["content"]
            != sms["content"]
        ):
            continue

        if (
            gespeicherte_sms["timestamp"]
            != sms["timestamp"]
        ):
            continue

        passende_sms.append(gespeicherte_sms)

    if len(passende_sms) != 1:
        return None

    return passende_sms[0]["cfg"]


# ============================================================
# SMS LÖSCHEN
# ============================================================

def loesche_sms(
    cfg,
    smsbox
):
    url = (
        f"{BASE_URL}"
        "/cgi-bin/luci/admin/network/gcom/sms/"
        f"delsms?iface=4g&cfg={cfg}"
    )

    headers = {
        **HEADERS,
        "Referer": (
            f"{BASE_URL}"
            "/cgi-bin/luci/admin/network/gcom/sms/"
            f"smslist?smsbox={smsbox}&iface=4g"
        )
    }

    try:

        response = session.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 403:

            raise PermissionError(
                "Session beim Löschen abgelaufen."
            )

        if response.status_code != 200:

            print(
                "❌ Löschen fehlgeschlagen:",
                response.status_code
            )

            return False

        print(
            "   🗑️ Gelöscht:",
            cfg
        )

        return True

    except PermissionError:
        raise

    except Exception as e:

        print(
            "❌ Löschfehler:",
            e
        )

        return False


# ============================================================
# ALLE SMS EINES POSTFACHS
# ============================================================

def hole_alle_sms_mit_cfg(
    smsbox
):
    seiten = hole_alle_sms_seiten(
        smsbox
    )

    alle_sms = []

    bereits_gefunden = set()

    for page, html in seiten.items():

        sms_liste = lese_sms_mit_cfg(
            html
        )

        for sms in sms_liste:

            identifikation = sms_id(
                sms
            )

            if identifikation in bereits_gefunden:
                continue

            bereits_gefunden.add(
                identifikation
            )

            sms["page"] = page

            alle_sms.append(
                sms
            )

    return alle_sms


# ============================================================
# CLEANUP
# ============================================================

def cleanup_smsbox(
    smsbox,
    start_limit,
    target_limit,
    geschützte_sms=None
):
    if geschützte_sms is None:
        geschützte_sms = set()

    print()

    print(
        f"🧹 Prüfe "
        f"{'INBOX' if smsbox == 'rec' else 'OUTBOX'}..."
    )

    sms_liste = hole_alle_sms_mit_cfg(
        smsbox
    )

    anzahl = len(
        sms_liste
    )

    print(
        "   📨 SMS:",
        anzahl
    )

    if smsbox == "rec":

        gui_status(
            "inbox",
            str(anzahl)
        )

    if anzahl < start_limit:

        print(
            f"   ✅ Unter Grenzwert "
            f"{start_limit}."
        )

        return

    zu_loeschen = (
        anzahl
        - target_limit
    )

    print(
        "   ⚠️ Speichergrenze erreicht."
    )

    print(
        f"   🗑️ Ziel: "
        f"{target_limit} SMS."
    )

    kandidaten = []

    for sms in reversed(
        sms_liste
    ):

        identifikation = sms_id(
            sms
        )

        if identifikation in geschützte_sms:

            print(
                "   🛡️ Geschützt:",
                sms["timestamp"]
            )

            continue

        kandidaten.append(
            sms
        )

        if len(kandidaten) >= zu_loeschen:
            break

    if not kandidaten:

        print(
            "   ⚠️ Keine löschbaren SMS gefunden."
        )

        return

    gelöscht = 0

    for sms in kandidaten:

        print()

        print(
            "🗑️ Cleanup:"
        )

        print(
            "   CFG:",
            sms["cfg"]
        )

        print(
            "   Zeit:",
            sms["timestamp"]
        )

        print(
            "   Absender:",
            sms["phone"]
        )

        print(
            "   Text:",
            sms["content"][:80]
        )

        if loesche_sms(
            sms["cfg"],
            smsbox
        ):

            gelöscht += 1

            time.sleep(
                DELETE_WAIT
            )

    print()

    print(
        f"🟢 Cleanup abgeschlossen. "
        f"{gelöscht} SMS gelöscht."
    )

    gui_status(
        "cleanup",
        f"{gelöscht} gelöscht"
    )


def cleanup(
    geschützte_sms=None
):
    if geschützte_sms is None:
        geschützte_sms = set()

    try:

        cleanup_smsbox(
            "rec",
            INBOX_CLEANUP_START,
            INBOX_CLEANUP_TARGET,
            geschützte_sms
        )

        cleanup_smsbox(
            "sto",
            OUTBOX_CLEANUP_START,
            OUTBOX_CLEANUP_TARGET,
            set()
        )

    except PermissionError:

        print()

        print(
            "🔐 Session beim Cleanup abgelaufen."
        )

        raise

    except Exception as e:

        print(
            "⚠️ Cleanup-Fehler:",
            e
        )


# ============================================================
# EIN POLL
# ============================================================

def poll():
    global letzter_erfolgreicher_poll

    html = hole_sms_seite(
        "rec",
        1
    )

    sms_liste = lese_sms(
        html
    )

    letzter_erfolgreicher_poll = (
        time.monotonic()
    )

    gui_status(
        "letzter_poll",
        time.strftime("%H:%M:%S")
    )

    gui_status(
        "inbox",
        str(len(sms_liste))
    )

    print(
        f"📡 Poll OK | "
        f"Inbox Seite 1: {len(sms_liste)} SMS"
    )

    geschützte_sms = verarbeite_sms_liste(
        sms_liste,
        html
    )

    return geschützte_sms


# ============================================================
# SESSION RECOVERY
# ============================================================

def session_recovery():
    global letzter_session_refresh
    global letzter_erfolgreicher_poll

    print()

    print(
        "🔄 SESSION-RECOVERY..."
    )

    gui_status(
        "status",
        "Session-Recovery"
    )

    session.cookies.clear()

    if login():

        letzter_session_refresh = (
            time.monotonic()
        )

        letzter_erfolgreicher_poll = (
            time.monotonic()
        )

        print(
            "🟢 Session-Recovery erfolgreich."
        )

        gui_status(
            "status",
            "Aktiv"
        )

        return True

    print(
        "🔴 Session-Recovery fehlgeschlagen."
    )

    gui_status(
        "status",
        "Fehler"
    )

    return False


# ============================================================
# BACKEND HAUPTSCHLEIFE
# ============================================================

def hauptschleife():
    global letzter_session_refresh
    global letzter_erfolgreicher_poll
    global poll_fehler

    print()

    print(
        "========================================"
    )

    print(
        "🚀 CUDY AUTO REFILL V4.1"
    )

    print(
        "🖥️ GUI MODUS"
    )

    print(
        "🔄 DAUERBETRIEB AKTIV"
    )

    print(
        f"⏱️ Poll: "
        f"{POLL_INTERVAL}s"
    )

    print(
        f"🔄 Session-Refresh: "
        f"{('Aus' if SESSION_REFRESH_INTERVAL == 0 else f'{SESSION_REFRESH_INTERVAL / 60:g}min')}"
    )

    print(
        f"🐕 Watchdog: "
        f"{WATCHDOG_TIMEOUT}s"
    )

    print(
        "🧠 SMS-Prüfung: "
        "max. 2 Beobachtungen / Refill-Versuche"
    )

    print(
        "🎯 Refill-Trigger:"
    )

    for trigger in REFILL_TRIGGERS:
        print(
            f"   • {trigger}"
        )

    print(
        f"🧹 Inbox Cleanup: "
        f"{INBOX_CLEANUP_START} → "
        f"{INBOX_CLEANUP_TARGET}"
    )

    print(
        f"🧹 Outbox Cleanup: "
        f"{OUTBOX_CLEANUP_START} → "
        f"{OUTBOX_CLEANUP_TARGET}"
    )

    print(
        "========================================"
    )

    gui_status(
        "status",
        "Verbinde..."
    )

    # --------------------------------------------------------
    # ERSTER LOGIN
    # --------------------------------------------------------

    while programm_laeuft:

        if login():
            break

        print()

        print(
            "⏳ Login fehlgeschlagen."
        )

        print(
            "🔄 Neuer Versuch in 10 Sekunden..."
        )

        for _ in range(10):

            if not programm_laeuft:
                return

            time.sleep(1)

    letzter_session_refresh = (
        time.monotonic()
    )

    letzter_erfolgreicher_poll = (
        time.monotonic()
    )

    # --------------------------------------------------------
    # DAUERLOOP
    # --------------------------------------------------------

    while programm_laeuft:

        try:

            geschützte_sms = set()

            # =================================================
            # SESSION REFRESH
            # =================================================

            if (
                SESSION_REFRESH_INTERVAL > 0
                and
                time.monotonic()
                - letzter_session_refresh
                >= SESSION_REFRESH_INTERVAL
            ):

                print()

                print(
                    "🔄 Regelmäßiger "
                    "Session-Refresh..."
                )

                if session_recovery():

                    poll_fehler = 0

                else:

                    poll_fehler += 1

            # =================================================
            # SMS POLL
            # =================================================

            try:

                geschützte_sms = poll()

                poll_fehler = 0

                gui_status(
                    "status",
                    "Aktiv"
                )

            except PermissionError:

                print()

                print(
                    "🔐 SESSION ABGELAUFEN!"
                )

                gui_status(
                    "status",
                    "Session abgelaufen"
                )

                if session_recovery():

                    poll_fehler = 0

                else:

                    poll_fehler += 1

            except requests.exceptions.Timeout:

                poll_fehler += 1

                print()

                print(
                    "⏰ REQUEST TIMEOUT!"
                )

                print(
                    f"   Poll-Fehler: "
                    f"{poll_fehler}/"
                    f"{MAX_POLL_FEHLER}"
                )

                gui_status(
                    "status",
                    "Timeout"
                )

            except requests.exceptions.RequestException as e:

                poll_fehler += 1

                print()

                print(
                    "🔴 Netzwerkfehler:",
                    e
                )

                print(
                    f"   Poll-Fehler: "
                    f"{poll_fehler}/"
                    f"{MAX_POLL_FEHLER}"
                )

                gui_status(
                    "status",
                    "Netzwerkfehler"
                )

            except Exception as e:

                poll_fehler += 1

                print()

                print(
                    "❌ Poll-Fehler:",
                    e
                )

                print(
                    f"   Poll-Fehler: "
                    f"{poll_fehler}/"
                    f"{MAX_POLL_FEHLER}"
                )

                gui_status(
                    "status",
                    "Poll-Fehler"
                )

            # =================================================
            # 3 FEHLER IN FOLGE
            # =================================================

            if poll_fehler >= MAX_POLL_FEHLER:

                print()

                print(
                    "🚨🚨🚨 WATCHDOG RECOVERY 🚨🚨🚨"
                )

                print(
                    "   → 3 Poll-Fehler in Folge."
                )

                gui_status(
                    "watchdog",
                    "Recovery"
                )

                if session_recovery():

                    poll_fehler = 0

                else:

                    print(
                        "⚠️ Recovery fehlgeschlagen."
                    )

                    poll_fehler = 0

            # =================================================
            # ZEIT-WATCHDOG
            # =================================================

            zeit_seit_poll = (
                time.monotonic()
                - letzter_erfolgreicher_poll
            )

            if (
                WATCHDOG_TIMEOUT > 0
                and
                zeit_seit_poll > WATCHDOG_TIMEOUT
            ):

                print()

                print(
                    "🐕 WATCHDOG!"
                )

                print(
                    f"   ⚠️ Seit "
                    f"{int(zeit_seit_poll)} Sekunden "
                    "kein erfolgreicher Poll."
                )

                gui_status(
                    "watchdog",
                    "Recovery"
                )

                if session_recovery():

                    poll_fehler = 0

                else:

                    poll_fehler = 0

            else:

                gui_status(
                    "watchdog",
                    "Aktiv"
                )

            # =================================================
            # CLEANUP
            # =================================================

            try:

                cleanup(
                    geschützte_sms
                )

            except PermissionError:

                print()

                print(
                    "🔐 Cleanup benötigt "
                    "neue Session."
                )

                session_recovery()

            # =================================================
            # WARTEN
            # =================================================

            print()

            if POLL_INTERVAL <= 0:
                print(
                    "💤 Automatischer Poll ist deaktiviert."
                )

                while programm_laeuft and POLL_INTERVAL <= 0:
                    time.sleep(1)

            else:
                print(
                    f"💤 Nächster Check in "
                    f"{POLL_INTERVAL} Sekunden..."
                )

                for _ in range(POLL_INTERVAL):

                    if not programm_laeuft:
                        break

                    time.sleep(1)

        except Exception as e:

            print()

            print(
                "💥 KRITISCHER FEHLER:",
                e
            )

            print(
                "🔄 Programm bleibt aktiv."
            )

            gui_status(
                "status",
                "Kritischer Fehler"
            )

            for _ in range(5):

                if not programm_laeuft:
                    break

                time.sleep(1)


# ============================================================
# GUI V4.2
# ============================================================

class CudyGUI:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "Cudy Auto Refill"
        )

        self.root.geometry(
            "1000x700"
        )

        self.root.minsize(
            850,
            600
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.schliessen
        )

        self.labels = {}

        # Einstellungen für die Überwachungsintervalle
        self.poll_interval_var = tk.StringVar(
            value=f"{POLL_INTERVAL} Sekunden"
        )
        self.session_refresh_var = tk.StringVar(
            value=f"{SESSION_REFRESH_INTERVAL // 60} Minuten"
        )
        self.watchdog_var = tk.StringVar(
            value=f"{WATCHDOG_TIMEOUT} Sekunden"
        )

        self.baue_gui()

        self.root.after(
            100,
            self.update_gui
        )

    # ========================================================
    # GUI AUFBAU
    # ========================================================

    def baue_gui(self):

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        header = ttk.Frame(
            self.root,
            padding=15
        )

        header.pack(
            fill="x"
        )

        title = ttk.Label(
            header,
            text="🚀 Cudy Auto Refill",
            font=(
                "Segoe UI",
                20,
                "bold"
            )
        )

        title.pack(
            side="left"
        )

        self.status_label = ttk.Label(
            header,
            text="● Gestoppt",
            font=(
                "Segoe UI",
                12,
                "bold"
            )
        )

        self.status_label.pack(
            side="right"
        )

        # ----------------------------------------------------
        # NOTEBOOK
        # ----------------------------------------------------

        self.notebook = ttk.Notebook(
            self.root
        )

        self.notebook.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 10)
        )

        # ====================================================
        # TAB 1 – ÜBERSICHT
        # ====================================================

        self.uebersicht_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.uebersicht_tab,
            text="🏠 Übersicht"
        )

        self.baue_uebersicht()

        # ====================================================
        # TAB 2 – SMS
        # ====================================================

        self.sms_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.sms_tab,
            text="💬 SMS"
        )

        self.baue_sms_tab()

        # ====================================================
        # TAB 3 – REFILL
        # ====================================================

        self.refill_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.refill_tab,
            text="🔄 Refill"
        )

        self.baue_refill_tab()

        # ====================================================
        # TAB 4 – SIGNAL
        # ====================================================

        self.signal_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.signal_tab,
            text="📡 Signal"
        )

        self.baue_signal_tab()

        # ====================================================
        # TAB 5 – EINSTELLUNGEN
        # ====================================================

        self.einstellungen_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.einstellungen_tab,
            text="⚙️ Einstellungen"
        )

        self.baue_einstellungen_tab()

        # ====================================================
        # TAB 6 – LOG
        # ====================================================

        self.log_tab = ttk.Frame(
            self.notebook,
            padding=10
        )

        self.notebook.add(
            self.log_tab,
            text="📜 Log"
        )

        self.baue_log_tab()

        # ----------------------------------------------------
        # START / STOP
        # ----------------------------------------------------

        button_frame = ttk.Frame(
            self.root,
            padding=(15, 5, 15, 10)
        )

        button_frame.pack(
            fill="x"
        )

        self.start_button = ttk.Button(
            button_frame,
            text="▶️ Start",
            command=self.starten
        )

        self.start_button.pack(
            side="left",
            padx=(0, 5)
        )

        self.stop_button = ttk.Button(
            button_frame,
            text="⏹ Stop",
            command=self.stoppen,
            state="disabled"
        )

        self.stop_button.pack(
            side="left"
        )

    # ========================================================
    # ÜBERSICHT
    # ========================================================

    def baue_uebersicht(self):

        tab = self.uebersicht_tab

        # ----------------------------------------------------
        # INFO
        # ----------------------------------------------------

        info = ttk.Label(
            tab,
            text="Systemübersicht",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        )

        info.pack(
            anchor="w",
            pady=(0, 10)
        )

        # ----------------------------------------------------
        # STATUS CARDS
        # ----------------------------------------------------

        status_frame = ttk.Frame(
            tab
        )

        status_frame.pack(
            fill="x"
        )

        karten = [
            ("status", "⚙️ Status"),
            ("router", "📡 Router"),
            ("inbox", "📥 Inbox"),
            ("letzter_refill", "⚡ Letzter Refill"),
            ("cleanup", "🧹 Cleanup"),
            ("watchdog", "🐕 Watchdog"),
            ("letzter_poll", "🔄 Letzter Poll"),
        ]

        for spalte, (key, titel) in enumerate(karten):

            frame = ttk.LabelFrame(
                status_frame,
                text=titel,
                padding=15
            )

            frame.grid(
                row=spalte // 3,
                column=spalte % 3,
                padx=5,
                pady=5,
                sticky="nsew"
            )

            label = ttk.Label(
                frame,
                text=status_data[key],
                font=(
                    "Segoe UI",
                    12,
                    "bold"
                )
            )

            label.pack()

            self.labels[key] = label

        for i in range(3):

            status_frame.columnconfigure(
                i,
                weight=1
            )

        # ----------------------------------------------------
        # SYSTEMINFO
        # ----------------------------------------------------

        system_frame = ttk.LabelFrame(
            tab,
            text="ℹ️ System",
            padding=15
        )

        system_frame.pack(
            fill="x",
            pady=(20, 0)
        )

        ttk.Label(
            system_frame,
            text=f"Poll-Intervall: {POLL_INTERVAL} Sekunden"
        ).pack(
            anchor="w"
        )

        ttk.Label(
            system_frame,
            text=(
                "Session-Refresh: "
                f"{SESSION_REFRESH_INTERVAL // 60} Minuten"
            )
        ).pack(
            anchor="w"
        )

        ttk.Label(
            system_frame,
            text=(
                "Watchdog: "
                f"{WATCHDOG_TIMEOUT} Sekunden"
            )
        ).pack(
            anchor="w"
        )

        ttk.Label(
            system_frame,
            text="Lidl Connect: 80808"
        ).pack(
            anchor="w"
        )

    # ========================================================
    # SMS TAB
    # ========================================================

    def baue_sms_tab(self):

        tab = self.sms_tab

        ttk.Label(
            tab,
            text="💬 SMS-Verwaltung",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        ).pack(
            anchor="w",
            pady=(0, 15)
        )

        # ----------------------------------------------------
        # POSTFÄCHER
        # ----------------------------------------------------

        box_frame = ttk.Frame(
            tab
        )

        box_frame.pack(
            fill="x"
        )

        inbox_frame = ttk.LabelFrame(
            box_frame,
            text="📥 Inbox",
            padding=20
        )

        inbox_frame.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 5)
        )

        self.sms_inbox_label = ttk.Label(
            inbox_frame,
            text=status_data["inbox"],
            font=(
                "Segoe UI",
                24,
                "bold"
            )
        )

        self.sms_inbox_label.pack()

        ttk.Label(
            inbox_frame,
            text="SMS im überwachten Postfach"
        ).pack()

        outbox_frame = ttk.LabelFrame(
            box_frame,
            text="📤 Outbox",
            padding=20
        )

        outbox_frame.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(5, 0)
        )

        self.sms_outbox_label = ttk.Label(
            outbox_frame,
            text="-",
            font=(
                "Segoe UI",
                24,
                "bold"
            )
        )

        self.sms_outbox_label.pack()

        ttk.Label(
            outbox_frame,
            text="Gesendete SMS"
        ).pack()

        # ----------------------------------------------------
        # INFO
        # ----------------------------------------------------

        info_frame = ttk.LabelFrame(
            tab,
            text="🔎 Überwachung",
            padding=15
        )

        info_frame.pack(
            fill="x",
            pady=(20, 0)
        )

        ttk.Label(
            info_frame,
            text=(
                "Überwacht wird Absender 80808."
            )
        ).pack(
            anchor="w"
        )

        ttk.Label(
            info_frame,
            text=(
                "Neue Nachrichten werden anhand "
                "des Refill-Trigger-Pools erkannt."
            )
        ).pack(
            anchor="w"
        )

        ttk.Label(
            info_frame,
            text=(
                "Aktuelle Trigger:"
            )
        ).pack(
            anchor="w",
            pady=(10, 0)
        )

        for trigger in REFILL_TRIGGERS:

            ttk.Label(
                info_frame,
                text=f"• {trigger}"
            ).pack(
                anchor="w"
            )

        # ----------------------------------------------------
        # AKTUALISIEREN
        # ----------------------------------------------------

        self.sms_refresh_button = ttk.Button(
            tab,
            text="🔄 Aktualisieren",
            command=self.sms_aktualisieren
        )

        self.sms_refresh_button.pack(
            anchor="w",
            pady=(20, 0)
        )

    # ========================================================
    # REFILL TAB
    # ========================================================

    def baue_refill_tab(self):

        tab = self.refill_tab

        ttk.Label(
            tab,
            text="🔄 Refill",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        ).pack(
            anchor="w",
            pady=(0, 15)
        )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        status_frame = ttk.LabelFrame(
            tab,
            text="⚡ Letzter Refill",
            padding=20
        )

        status_frame.pack(
            fill="x"
        )

        self.refill_status_label = ttk.Label(
            status_frame,
            text=status_data["letzter_refill"],
            font=(
                "Segoe UI",
                24,
                "bold"
            )
        )

        self.refill_status_label.pack()

        # ----------------------------------------------------
        # KONFIGURATION
        # ----------------------------------------------------

        config_frame = ttk.LabelFrame(
            tab,
            text="🎯 Refill-Konfiguration",
            padding=15
        )

        config_frame.pack(
            fill="x",
            pady=(20, 0)
        )

        ttk.Label(
            config_frame,
            text="Antwort:"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 15)
        )

        ttk.Label(
            config_frame,
            text=REFILL_TEXT,
            font=(
                "Segoe UI",
                10,
                "bold"
            )
        ).grid(
            row=0,
            column=1,
            sticky="w"
        )

        ttk.Label(
            config_frame,
            text="Absender:"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 15),
            pady=(8, 0)
        )

        ttk.Label(
            config_frame,
            text=LIDL_ABSENDER
        ).grid(
            row=1,
            column=1,
            sticky="w",
            pady=(8, 0)
        )

        # ----------------------------------------------------
        # HINWEIS
        # ----------------------------------------------------

        hinweis = ttk.LabelFrame(
            tab,
            text="ℹ️ Automatik",
            padding=15
        )

        hinweis.pack(
            fill="x",
            pady=(20, 0)
        )

        ttk.Label(
            hinweis,
            text=(
                "Ein Refill wird erst als erfolgreich "
                "gewertet, wenn die neue Refill-SMS "
                "in der Outbox bestätigt wurde."
            ),
            wraplength=800
        ).pack(
            anchor="w"
        )

        ttk.Label(
            hinweis,
            text=(
                "Pro Trigger-SMS sind maximal zwei "
                "Beobachtungen bzw. Refill-Versuche möglich."
            ),
            wraplength=800
        ).pack(
            anchor="w",
            pady=(8, 0)
        )

        # ----------------------------------------------------
        # TEST BUTTON
        # ----------------------------------------------------

        self.test_refill_button = ttk.Button(
            tab,
            text="🚨 Manueller Notfall-Refill senden",
            command=self.test_refill
        )

        self.test_refill_button.pack(
            anchor="w",
            pady=(20, 0)
        )

    # ========================================================
    # SIGNAL TAB
    # ========================================================

    def baue_signal_tab(self):

        tab = self.signal_tab

        ttk.Label(
            tab,
            text="📡 Signal & Netzwerk",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        ).pack(
            anchor="w",
            pady=(0, 15)
        )

        # ----------------------------------------------------
        # AKTUELL NOCH NICHT AUSGELESEN
        # ----------------------------------------------------

        signal_frame = ttk.LabelFrame(
            tab,
            text="📶 Mobilfunksignal",
            padding=20
        )

        signal_frame.pack(
            fill="x"
        )

        self.signal_value = ttk.Label(
            signal_frame,
            text="Noch nicht ausgelesen",
            font=(
                "Segoe UI",
                18,
                "bold"
            )
        )

        self.signal_value.pack()

        # ----------------------------------------------------
        # NETZWERK
        # ----------------------------------------------------

        network_frame = ttk.LabelFrame(
            tab,
            text="🌐 Netzwerk",
            padding=15
        )

        network_frame.pack(
            fill="x",
            pady=(20, 0)
        )

        ttk.Label(
            network_frame,
            text="Verbindung:"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 20)
        )

        self.network_value = ttk.Label(
            network_frame,
            text="Cudy LT300 V3 / 4G"
        )

        self.network_value.grid(
            row=0,
            column=1,
            sticky="w"
        )

        ttk.Label(
            network_frame,
            text="Interface:"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 20),
            pady=(8, 0)
        )

        ttk.Label(
            network_frame,
            text="4g"
        ).grid(
            row=1,
            column=1,
            sticky="w",
            pady=(8, 0)
        )

        # ----------------------------------------------------
        # UPDATE
        # ----------------------------------------------------

        self.signal_refresh_button = ttk.Button(
            tab,
            text="🔄 Aktualisieren",
            command=self.signal_aktualisieren
        )

        self.signal_refresh_button.pack(
            anchor="w",
            pady=(20, 0)
        )

        ttk.Label(
            tab,
            text=(
                "Die automatische Signalüberwachung "
                "wird im nächsten Schritt mit den "
                "Cudy-Netzwerkdaten verbunden."
            ),
            wraplength=800
        ).pack(
            anchor="w",
            pady=(15, 0)
        )

    # ========================================================
    # EINSTELLUNGEN TAB
    # ========================================================

    def baue_einstellungen_tab(self):

        tab = self.einstellungen_tab

        ttk.Label(
            tab,
            text="⚙️ Einstellungen",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        ).pack(
            anchor="w",
            pady=(0, 15)
        )

        # ----------------------------------------------------
        # ROUTER
        # ----------------------------------------------------

        router_frame = ttk.LabelFrame(
            tab,
            text="📡 Router",
            padding=15
        )

        router_frame.pack(
            fill="x"
        )

        ttk.Label(
            router_frame,
            text="Router-Adresse:"
        ).grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.router_entry = ttk.Entry(
            router_frame,
            width=35
        )

        self.router_entry.insert(
            0,
            BASE_URL
        )

        self.router_entry.grid(
            row=0,
            column=1,
            padx=10
        )

        # ----------------------------------------------------
        # SMS
        # ----------------------------------------------------

        sms_frame = ttk.LabelFrame(
            tab,
            text="💬 SMS / Refill",
            padding=15
        )

        sms_frame.pack(
            fill="x",
            pady=(15, 0)
        )

        ttk.Label(
            sms_frame,
            text="Lidl-Absender:"
        ).grid(
            row=0,
            column=0,
            sticky="w"
        )

        ttk.Label(
            sms_frame,
            text=LIDL_ABSENDER
        ).grid(
            row=0,
            column=1,
            sticky="w",
            padx=10
        )

        ttk.Label(
            sms_frame,
            text="Antwort:"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0)
        )

        ttk.Label(
            sms_frame,
            text=REFILL_TEXT
        ).grid(
            row=1,
            column=1,
            sticky="w",
            padx=10,
            pady=(8, 0)
        )

        # ----------------------------------------------------
        # POLLING
        # ----------------------------------------------------

        polling_frame = ttk.LabelFrame(
            tab,
            text="⏱️ Überwachung",
            padding=15
        )

        polling_frame.pack(
            fill="x",
            pady=(15, 0)
        )

        ttk.Label(
            polling_frame,
            text="Poll-Intervall:"
        ).grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.poll_interval_combo = ttk.Combobox(
            polling_frame,
            textvariable=self.poll_interval_var,
            values=[
                "5 Sekunden",
                "10 Sekunden",
                "15 Sekunden",
                "30 Sekunden",
                "60 Sekunden",
                "120 Sekunden",
                "300 Sekunden",
            ],
            state="readonly",
            width=18
        )
        self.poll_interval_combo.grid(
            row=0,
            column=1,
            sticky="w",
            padx=10
        )

        ttk.Label(
            polling_frame,
            text="Session-Refresh:"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0)
        )

        self.session_refresh_combo = ttk.Combobox(
            polling_frame,
            textvariable=self.session_refresh_var,
            values=[
                "Aus",
                "1 Minute",
                "2 Minuten",
                "5 Minuten",
                "10 Minuten",
                "15 Minuten",
                "30 Minuten",
                "60 Minuten",
            ],
            state="readonly",
            width=18
        )
        self.session_refresh_combo.grid(
            row=1,
            column=1,
            sticky="w",
            padx=10,
            pady=(8, 0)
        )

        ttk.Label(
            polling_frame,
            text="Watchdog:"
        ).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(8, 0)
        )

        self.watchdog_combo = ttk.Combobox(
            polling_frame,
            textvariable=self.watchdog_var,
            values=[
                "Aus",
                "30 Sekunden",
                "45 Sekunden",
                "60 Sekunden",
                "90 Sekunden",
                "120 Sekunden",
                "180 Sekunden",
                "300 Sekunden",
            ],
            state="readonly",
            width=18
        )
        self.watchdog_combo.grid(
            row=2,
            column=1,
            sticky="w",
            padx=10,
            pady=(8, 0)
        )

        # ----------------------------------------------------
        # BÄNDER
        # ----------------------------------------------------

        band_frame = ttk.LabelFrame(
            tab,
            text="📶 LTE-Bänder",
            padding=15
        )

        band_frame.pack(
            fill="x",
            pady=(15, 0)
        )

        ttk.Label(
            band_frame,
            text=(
                "LTE-Band-Auswahl wird im nächsten "
                "Schritt mit der Cudy-API verbunden."
            ),
            wraplength=800
        ).pack(
            anchor="w"
        )

        self.band_button = ttk.Button(
            band_frame,
            text="📶 Bänder verwalten",
            command=self.baender_verwalten
        )

        self.band_button.pack(
            anchor="w",
            pady=(10, 0)
        )

        # ----------------------------------------------------
        # SPEICHERN
        # ----------------------------------------------------

        self.settings_save_button = ttk.Button(
            tab,
            text="💾 Einstellungen speichern",
            command=self.einstellungen_speichern
        )

        self.settings_save_button.pack(
            anchor="w",
            pady=(20, 0)
        )

    # ========================================================
    # LOG TAB
    # ========================================================

    def baue_log_tab(self):

        tab = self.log_tab

        # ----------------------------------------------------
        # BUTTONS
        # ----------------------------------------------------

        button_frame = ttk.Frame(
            tab
        )

        button_frame.pack(
            fill="x",
            pady=(0, 10)
        )

        self.log_clear_button = ttk.Button(
            button_frame,
            text="🗑️ Log löschen",
            command=self.log_loeschen
        )

        self.log_clear_button.pack(
            side="left"
        )

        # ----------------------------------------------------
        # LOG
        # ----------------------------------------------------

        log_frame = ttk.Frame(
            tab
        )

        log_frame.pack(
            fill="both",
            expand=True
        )

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            font=(
                "Consolas",
                9
            ),
            state="disabled"
        )

        scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview
        )

        self.log_text.configure(
            yscrollcommand=scrollbar.set
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        self.log_text.pack(
            side="left",
            fill="both",
            expand=True
        )

    # ========================================================
    # START
    # ========================================================

    def starten(self):

        global programm_laeuft
        global backend_thread

        if programm_laeuft:
            return

        programm_laeuft = True

        self.start_button.config(
            state="disabled"
        )

        self.stop_button.config(
            state="normal"
        )

        gui_status(
            "status",
            "Starte..."
        )

        backend_thread = threading.Thread(
            target=hauptschleife,
            daemon=True
        )

        backend_thread.start()

    # ========================================================
    # STOP
    # ========================================================

    def stoppen(self):

        global programm_laeuft

        if not programm_laeuft:
            return

        print()

        print(
            "🛑 Stop angefordert..."
        )

        programm_laeuft = False

        gui_status(
            "status",
            "Wird beendet..."
        )

        self.stop_button.config(
            state="disabled"
        )

    # ========================================================
    # SMS AKTUALISIEREN
    # ========================================================

    def sms_aktualisieren(self):

        if not programm_laeuft:

            print(
                "⚠️ Programm muss laufen, "
                "um SMS zu aktualisieren."
            )

            return

        print()
        print(
            "🔄 Manuelle SMS-Aktualisierung..."
        )

        def worker():

            try:

                html = hole_sms_seite(
                    "rec",
                    1
                )

                sms_liste = lese_sms(
                    html
                )

                gui_status(
                    "inbox",
                    str(len(sms_liste))
                )

                print(
                    f"🟢 SMS-Aktualisierung: "
                    f"{len(sms_liste)} SMS"
                )

            except Exception as e:

                print(
                    "❌ SMS-Aktualisierung fehlgeschlagen:",
                    e
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    # ========================================================
    # SIGNAL AKTUALISIEREN
    # ========================================================

    def signal_aktualisieren(self):

        self.signal_value.config(
            text="Noch nicht implementiert"
        )

        print(
            "📡 Signal-Aktualisierung angefordert."
        )

    # ========================================================
    # TEST REFILL
    # ========================================================

    def test_refill(self):
        if not messagebox.askyesno(
            "Notfall-Refill senden?",
            f"Jetzt '{REFILL_TEXT}' per SMS an {LIDL_ABSENDER} senden?\n\n"
            "Dadurch kann beim Mobilfunkanbieter ein Refill ausgelöst werden.",
            parent=self.root
        ):
            return

        self.test_refill_button.config(state="disabled")

        def worker():
            print("🚨 Manueller Refill gestartet: sende 'Refill' an 80808.")
            try:
                with REFILL_SEND_LOCK:
                    if not router_session_sicherstellen():
                        print("❌ Manueller Refill fehlgeschlagen: keine gültige Router-Session.")
                        return

                    vorher = outbox_snapshot()
                    if not sende_neue_sms(LIDL_ABSENDER, REFILL_TEXT):
                        print("❌ Manueller Refill fehlgeschlagen: SMS wurde nicht an den Router übermittelt.")
                        return

                    time.sleep(3)
                    nachher = outbox_snapshot()

                if nachher - vorher:
                    gui_status("letzter_refill", time.strftime("%H:%M:%S"))
                    print("✅ Manueller Refill erfolgreich: neue Refill-SMS in der Outbox bestätigt.")
                else:
                    print("⚠️ Manueller Refill unbestätigt: Router nahm die SMS an, aber die Outbox zeigt keinen neuen Refill.")
            except Exception as exc:
                print("❌ Manueller Refill fehlgeschlagen:", exc)
            finally:
                try:
                    self.root.after(0, lambda: self.test_refill_button.config(state="normal"))
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # BÄNDER
    # ========================================================

    def baender_verwalten(self):

        fenster = tk.Toplevel(
            self.root
        )

        fenster.title(
            "📶 LTE-Bänder"
        )

        fenster.geometry(
            "400x350"
        )

        fenster.transient(
            self.root
        )

        ttk.Label(
            fenster,
            text="LTE-Bänder",
            font=(
                "Segoe UI",
                16,
                "bold"
            )
        ).pack(
            pady=(20, 10)
        )

        ttk.Label(
            fenster,
            text=(
                "Die eigentliche Cudy-Bandsteuerung "
                "kommt nach dem Auslesen der "
                "entsprechenden Router-API."
            ),
            wraplength=350,
            justify="center"
        ).pack(
            padx=20
        )

        ttk.Button(
            fenster,
            text="Schließen",
            command=fenster.destroy
        ).pack(
            pady=20
        )

    # ========================================================
    # EINSTELLUNGEN SPEICHERN
    # ========================================================

    def einstellungen_speichern(self):

        global BASE_URL
        global POLL_INTERVAL
        global SESSION_REFRESH_INTERVAL
        global WATCHDOG_TIMEOUT
        global letzter_session_refresh
        global letzter_erfolgreicher_poll

        def sekunden_aus_text(wert):
            if wert == "Aus":
                return 0

            zahl = int(wert.split()[0])

            # Poll/Watchdog sind in Sekunden, Session-Refresh in Minuten.
            if "Minute" in wert:
                return zahl * 60

            return zahl

        neuer_poll = sekunden_aus_text(
            self.poll_interval_var.get()
        )
        neuer_session_refresh = sekunden_aus_text(
            self.session_refresh_var.get()
        )
        neuer_watchdog = sekunden_aus_text(
            self.watchdog_var.get()
        )

        BASE_URL = self.router_entry.get().strip() or BASE_URL
        POLL_INTERVAL = neuer_poll
        SESSION_REFRESH_INTERVAL = neuer_session_refresh
        WATCHDOG_TIMEOUT = neuer_watchdog

        # Zeitgeber beim Speichern neu starten, damit die
        # neue Einstellung sofort und ohne alten Restwert greift.
        jetzt = time.monotonic()
        letzter_session_refresh = jetzt
        letzter_erfolgreicher_poll = jetzt

        print()
        print("💾 Einstellungen gespeichert:")
        print("   Router:", BASE_URL)
        print("   Poll:", "Aus" if POLL_INTERVAL == 0 else f"{POLL_INTERVAL} Sekunden")
        print(
            "   Session-Refresh:",
            "Aus" if SESSION_REFRESH_INTERVAL == 0
            else f"{SESSION_REFRESH_INTERVAL / 60:g} Minuten"
        )
        print(
            "   Watchdog:",
            "Aus" if WATCHDOG_TIMEOUT == 0
            else f"{WATCHDOG_TIMEOUT} Sekunden"
        )

        gui_status(
            "status",
            "Einstellungen gespeichert"
        )

    # ========================================================
    # LOG LÖSCHEN
    # ========================================================

    def log_loeschen(self):

        self.log_text.config(
            state="normal"
        )

        self.log_text.delete(
            "1.0",
            "end"
        )

        self.log_text.config(
            state="disabled"
        )

        print(
            "🗑️ GUI-Log gelöscht."
        )

    # ========================================================
    # GUI UPDATE
    # ========================================================

    def update_gui(self):

        try:

            while True:

                item = log_queue.get_nowait()

                # --------------------------------------------
                # LOG
                # --------------------------------------------

                if item[0] == "log":

                    text = item[1]

                    self.log_text.config(
                        state="normal"
                    )

                    self.log_text.insert(
                        "end",
                        text
                    )

                    self.log_text.see(
                        "end"
                    )

                    self.log_text.config(
                        state="disabled"
                    )

                # --------------------------------------------
                # STATUS
                # --------------------------------------------

                elif item[0] == "status":

                    key = item[1]
                    value = item[2]

                    status_data[key] = value

                    if key in self.labels:

                        self.labels[key].config(
                            text=value
                        )

                    if key == "inbox":

                        self.sms_inbox_label.config(
                            text=value
                        )

                    if key == "letzter_refill":

                        self.refill_status_label.config(
                            text=value
                        )

        except queue.Empty:
            pass

        # ----------------------------------------------------
        # HEADER STATUS
        # ----------------------------------------------------

        status = status_data["status"]

        if status == "Aktiv":

            self.status_label.config(
                text="● Aktiv"
            )

        elif status in (
            "Fehler",
            "Timeout",
            "Netzwerkfehler",
            "Poll-Fehler",
            "Kritischer Fehler"
        ):

            self.status_label.config(
                text="● Fehler"
            )

        elif status == "Gestoppt":

            self.status_label.config(
                text="● Gestoppt"
            )

        else:

            self.status_label.config(
                text=f"● {status}"
            )

        # ----------------------------------------------------
        # STOP BUTTON NACH BACKEND-ENDE
        # ----------------------------------------------------

        global programm_laeuft

        if not programm_laeuft:

            self.start_button.config(
                state="normal"
            )

            self.stop_button.config(
                state="disabled"
            )

            if status_data["status"] != "Gestoppt":

                status_data["status"] = "Gestoppt"

                self.labels["status"].config(
                    text="Gestoppt"
                )

                self.status_label.config(
                    text="● Gestoppt"
                )

        self.root.after(
            100,
            self.update_gui
        )

    # ========================================================
    # SCHLIESSEN
    # ========================================================

    def schliessen(self):

        global programm_laeuft

        if programm_laeuft:

            programm_laeuft = False

            print()

            print(
                "🛑 Cudy Auto Refill wird beendet..."
            )

        self.root.destroy()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    import sys

    original_stdout = sys.stdout

    sys.stdout = QueueLogger(
        original_stdout
    )

    root = tk.Tk()

    app = CudyGUI(
        root
    )

    root.mainloop()