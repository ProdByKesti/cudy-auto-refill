import requests
import hashlib
import time
import json
import os
import sys
import ctypes
import shutil
import threading
import queue
import logging
import re
import unicodedata
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import ttk, messagebox
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from lte_features import (
    LTE_BANDS,
    apply_band_config_with_fallback,
    read_band_config,
    run_all_band_speedtest,
)

# ============================================================
# KONFIGURATION
# ============================================================

BASE_URL = "http://192.168.10.1"
APP_HOME_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "CudyAutoRefill",
)
CREDENTIALS_FILE = os.path.join(APP_DATA_DIR, "router_password.dpapi")
LEGACY_CREDENTIAL_FILES = (
    os.path.join(APP_DATA_DIR, "cudy_credentials.json"),
    os.path.join(APP_HOME_DIR, "cudy_credentials.json"),
)
SMS_STATE_FILE = os.path.join(APP_DATA_DIR, "sms_state.json")
LEGACY_SMS_STATE_FILE = os.path.join(APP_HOME_DIR, "sms_state.json")
LTE_PROFILE_FILE = os.path.join(APP_DATA_DIR, "lte_profile.json")
APP_LOG_FILE = os.path.join(APP_DATA_DIR, "cudy_auto_refill.log")
SETTINGS_FILE = os.path.join(APP_DATA_DIR, "settings.json")
MAX_GUI_LOG_LINES = 3000


def lade_app_einstellungen():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


APP_SETTINGS = lade_app_einstellungen()
_saved_router_url = APP_SETTINGS.get("router_url")
if isinstance(_saved_router_url, str) and _saved_router_url.startswith(("http://", "https://")):
    BASE_URL = _saved_router_url.rstrip("/")


def _setting_int(name, default, minimum, maximum):
    try:
        value = int(APP_SETTINGS.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def speichere_app_einstellungen(value):
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        temporary = SETTINGS_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_FILE)
        return True
    except OSError as exc:
        print("Einstellungen konnten nicht gespeichert werden:", exc)
        return False


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_transform(data, protect):
    if os.name != "nt":
        raise OSError("Windows DPAPI ist auf diesem System nicht verfügbar.")
    raw = bytes(data)
    buffer = ctypes.create_string_buffer(raw, len(raw))
    input_blob = _DataBlob(
        len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.POINTER(_DataBlob),
        ]
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.POINTER(_DataBlob),
        ]
    function.restype = ctypes.c_bool
    if not function(
        ctypes.byref(input_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(output_blob.pbData)


def speichere_router_passwort(passwort):
    encrypted = _dpapi_transform(passwort.encode("utf-8"), protect=True)
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    temporary = CREDENTIALS_FILE + ".tmp"
    with open(temporary, "wb") as f:
        f.write(encrypted)
    os.replace(temporary, CREDENTIALS_FILE)


def lade_router_passwort():
    passwort = os.environ.get("CUDY_ROUTER_PASSWORD")
    if passwort:
        return passwort

    try:
        with open(CREDENTIALS_FILE, "rb") as f:
            encrypted = f.read()
        return _dpapi_transform(encrypted, protect=False).decode("utf-8")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeDecodeError):
        print("Geschütztes Router-Passwort konnte nicht gelesen werden.")

    for path in LEGACY_CREDENTIAL_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        passwort = data.get("router_password", "")
        if not isinstance(passwort, str) or not passwort:
            continue
        if os.name == "nt":
            try:
                speichere_router_passwort(passwort)
                print(
                    "Router-Passwort in Windows DPAPI übernommen. "
                    "Die alte JSON-Datei bleibt zur manuellen Entfernung erhalten."
                )
            except (OSError, ValueError):
                print("Router-Passwort konnte nicht mit Windows DPAPI geschützt werden.")
        return passwort
    return ""


# The password is loaded lazily when the first router login is attempted.
PASSWORT = ""

def lade_lte_profil():
    try:
        with open(LTE_PROFILE_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def speichere_lte_profil(value):
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        temporary = LTE_PROFILE_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temporary, LTE_PROFILE_FILE)
    except OSError as exc:
        print("LTE-Profil konnte nicht gespeichert werden:", exc)


# Polling
POLL_INTERVAL = _setting_int("poll_interval", 10, 1, 3600)

# Regelmäßiger Session-Refresh
SESSION_REFRESH_INTERVAL = _setting_int("session_refresh_interval", 300, 0, 86400)

# Netzwerk-Timeout
REQUEST_TIMEOUT = 10

# Watchdog
MAX_POLL_FEHLER = 3
WATCHDOG_TIMEOUT = _setting_int("watchdog_timeout", 45, 0, 86400)
LOGIN_RETRY_BASE_SECONDS = 10
LOGIN_RETRY_MAX_SECONDS = 120
SESSION_RECOVERY_BASE_SECONDS = 10
SESSION_RECOVERY_MAX_SECONDS = 120

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


def normalisiere_sms_text(text):
    """Normalize SMS text before matching router-rendered content."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.casefold().strip()


def ist_refill_trigger(text):
    normalized = normalisiere_sms_text(text)
    if any(normalisiere_sms_text(trigger) in normalized for trigger in REFILL_TRIGGERS):
        return True

    # Lidl Connect varies the wording of the exhausted Unlimited-1GB notice.
    return (
        "unlimited" in normalized
        and "option" in normalized
        and re.search(r"\bverbrauch\w*", normalized) is not None
    )


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
    "letzter_poll": "-",
    "fehler": ""
}


def redact_log_line(text):
    """Remove credential-like values and SMS bodies before writing diagnostics."""
    text = re.sub(
        r"(?i)([\w-]*(?:password|passwort|token|csrf|salt|privatekey)[\w-]*\s*[:=]\s*)[^,\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:token|csrf|password|passwort)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    match = re.search(r"(?i)\bText:\s*", text)
    if match:
        text = text[:match.start()] + match.group(0) + "[SMS-Inhalt ausgelassen]"
    return text


class QueueLogger:
    """Forward print output to the console, GUI, and a rotating redacted log."""

    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
        self._buffer = ""
        self._lock = threading.RLock()
        self.file_logger = logging.getLogger("cudy_auto_refill")
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False
        if not self.file_logger.handlers:
            try:
                os.makedirs(APP_DATA_DIR, exist_ok=True)
                handler = RotatingFileHandler(
                    APP_LOG_FILE, maxBytes=1_000_000, backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                ))
                self.file_logger.addHandler(handler)
            except OSError:
                self.file_logger = None

    def write(self, value):
        if not value:
            return
        with self._lock:
            self.original_stdout.write(value)
            self.original_stdout.flush()
            log_queue.put(("log", value))
            self._buffer += value
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if self.file_logger:
                    self.file_logger.info(redact_log_line(line.rstrip("\r")))

    def flush(self):
        with self._lock:
            self.original_stdout.flush()
            if self._buffer:
                if self.file_logger:
                    self.file_logger.info(redact_log_line(self._buffer))
                self._buffer = ""
            if self.file_logger:
                for handler in self.file_logger.handlers:
                    handler.flush()
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
session_recovery_retry_at = 0.0
session_recovery_retry_delay = SESSION_RECOVERY_BASE_SECONDS


# ============================================================
# GUI STATUS HELFER
# ============================================================

def gui_status(name, value):
    status_data[name] = value

    log_queue.put(
        ("status", name, value)
    )


FEHLER_PHASEN = {
    "login": "Router-Login",
    "poll": "Inbox-Polling",
    "outbox": "Refill-Bestätigung",
}
AKTIVE_FEHLER = {}
AKTIVE_FEHLER_TYPEN = {}
AKTIVE_FEHLER_LOCK = threading.Lock()


def _fehlertext_locked():
    return "\n".join(
        f"{FEHLER_PHASEN[phase]}: {AKTIVE_FEHLER[phase]}"
        for phase in FEHLER_PHASEN
        if phase in AKTIVE_FEHLER
    )


def melde_fehler(phase, message, art=None):
    if phase not in FEHLER_PHASEN:
        return False

    with AKTIVE_FEHLER_LOCK:
        vorherige_meldung = AKTIVE_FEHLER.get(phase)
        vorherige_art = AKTIVE_FEHLER_TYPEN.get(phase)
        if vorherige_meldung == message and (art is None or vorherige_art == art):
            return False

        AKTIVE_FEHLER[phase] = message
        if art is not None:
            AKTIVE_FEHLER_TYPEN[phase] = art
        text = _fehlertext_locked()

    gui_status("fehler", text)
    return True


def bereinige_fehler(phase, nur_art=None):
    with AKTIVE_FEHLER_LOCK:
        if phase not in AKTIVE_FEHLER:
            return False
        if nur_art is not None and AKTIVE_FEHLER_TYPEN.get(phase) != nur_art:
            return False

        del AKTIVE_FEHLER[phase]
        AKTIVE_FEHLER_TYPEN.pop(phase, None)
        text = _fehlertext_locked()

    gui_status("fehler", text)
    return True


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

    print("Router-Token erhalten.")

    return token


def login():
    global PASSWORT
    print()
    print("Starte Cudy Login...")

    if not PASSWORT:
        PASSWORT = lade_router_passwort()
    if not PASSWORT:
        print("Router-Passwort fehlt. Lege es in cudy_credentials.json oder CUDY_ROUTER_PASSWORD ab.")
        gui_status("router", "Passwort fehlt")
        gui_status("status", "Login-Fehler")
        melde_fehler("login", "Router-Passwort fehlt. Bitte in den Einstellungen hinterlegen.")
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

        # Cudy-Firmware kann nach erfolgreichem Login direkt HTTP 200 statt
        # eines Redirects liefern. Entscheidend ist, ob eine geschützte Seite
        # ohne erneutes Login-Formular erreichbar ist.
        sms_page = session.get(
            f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms?iface=4g",
            timeout=REQUEST_TIMEOUT
        )
        login_form = BeautifulSoup(sms_page.text, "html.parser").find(
            "input", {"name": "luci_password"}
        )

        if sms_page.status_code == 200 and not login_form:
            print("LOGIN ERFOLGREICH! (geschützte Router-Seite bestätigt)")

            gui_status(
                "router",
                "Verbunden"
            )

            gui_status(
                "status",
                "Aktiv"
            )
            bereinige_fehler("login")

            return True

        grund = (
            "Router zeigt weiterhin das Login-Formular"
            if login_form
            else f"geschützte Seite antwortet mit HTTP {sms_page.status_code}"
        )
        print(
            f"LOGIN FEHLGESCHLAGEN: POST HTTP {login_response.status_code}; {grund}."
        )

        gui_status(
            "router",
            "Login fehlgeschlagen"
        )
        gui_status("status", "Login-Fehler")
        melde_fehler("login", "Anmeldung am Router fehlgeschlagen; automatische Wiederholungen laufen.")

        return False

    except requests.exceptions.RequestException as e:
        print(
            "LOGIN-NETZWERKFEHLER:",
            e
        )

        gui_status(
            "router",
            "Netzwerkfehler"
        )
        gui_status("status", "Login-Fehler")
        melde_fehler("login", "Router ist beim Login nicht erreichbar; automatische Wiederholungen laufen.")

        return False

    except Exception as e:
        print(
            "LOGIN-FEHLER:",
            e
        )

        gui_status(
            "router",
            "Fehler"
        )
        gui_status("status", "Login-Fehler")
        melde_fehler("login", "Router-Login ist fehlgeschlagen; automatische Wiederholungen laufen.")

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
    if BeautifulSoup(response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    ):
        raise PermissionError("Cudy-Session abgelaufen (Login-Seite mit HTTP 200).")

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
        f"   {smsbox.upper()}: "
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
                f"Seite {page} konnte "
                f"nicht geladen werden:",
                e
            )

    return seiten


# ============================================================
# SMS AUS HTML PARSEN
# ============================================================

def sms_element_text(element):
    """Prefer the longest visible or tooltip text available in a table cell."""
    if not element:
        return ""

    candidates = [element.get_text(" ", strip=True)]
    for node in [element, *element.find_all(True)]:
        for attribute in (
            "title",
            "aria-label",
            "data-content",
            "data-original-title",
            "data-fulltext",
            "data-full-text",
        ):
            value = node.get(attribute)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

    return max(candidates, key=len, default="")


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
                sms_element_text(content_element),

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

        content_text = sms_element_text(content_element)

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

    if (
        not os.path.exists(SMS_STATE_FILE)
        and os.path.exists(LEGACY_SMS_STATE_FILE)
    ):
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            shutil.copy2(LEGACY_SMS_STATE_FILE, SMS_STATE_FILE)
            print("Vorhandene SMS-States ins Windows-Benutzerprofil übernommen.")
        except OSError as exc:
            print("SMS-States konnten nicht übernommen werden:", exc)

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
            "sms_state.json konnte "
            "nicht gelesen werden:",
            e
        )

        return {}


def speichere_sms_state(state):

    os.makedirs(APP_DATA_DIR, exist_ok=True)
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

OUTBOX_CONFIRMATION_DELAYS = (2, 4, 6)
OUTBOX_PENDING_CHECK_INTERVAL = 30
OUTBOX_PENDING_TIMEOUT = 300
OUTBOX_PENDING_BASELINE = None
OUTBOX_PENDING_EXPIRES_AT = 0.0
OUTBOX_PENDING_NEXT_CHECK = 0.0
OUTBOX_PENDING_LOCK = threading.Lock()


class OutboxSnapshotError(Exception):
    """The Outbox baseline could not be read before attempting a refill."""


def merke_ausstehende_outbox_bestaetigung(baseline):
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK

    with OUTBOX_PENDING_LOCK:
        OUTBOX_PENDING_BASELINE = set(baseline)
        OUTBOX_PENDING_EXPIRES_AT = time.monotonic() + OUTBOX_PENDING_TIMEOUT
        OUTBOX_PENDING_NEXT_CHECK = time.monotonic() + OUTBOX_PENDING_CHECK_INTERVAL


def pruefe_ausstehende_outbox_bestaetigung(snapshot):
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK

    with OUTBOX_PENDING_LOCK:
        if OUTBOX_PENDING_BASELINE is None:
            return False

        if time.monotonic() >= OUTBOX_PENDING_EXPIRES_AT:
            OUTBOX_PENDING_BASELINE = None
            OUTBOX_PENDING_EXPIRES_AT = 0.0
            OUTBOX_PENDING_NEXT_CHECK = 0.0
            return False

        neue_eintraege = snapshot - OUTBOX_PENDING_BASELINE
        if not neue_eintraege:
            print(
                "Outbox-Bestätigung offen: "
                f"passende Refill-Einträge={len(snapshot)}, neue=0."
            )
            return False

        OUTBOX_PENDING_BASELINE = None
        OUTBOX_PENDING_EXPIRES_AT = 0.0
        OUTBOX_PENDING_NEXT_CHECK = 0.0

    gui_status("letzter_refill", time.strftime("%H:%M:%S"))
    bereinige_fehler("outbox", nur_art="confirmation")
    print("Verzögerte Refill-Bestätigung in der Outbox erkannt.")
    return True


def pruefe_offene_outbox_bestaetigung():
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK

    with OUTBOX_PENDING_LOCK:
        if OUTBOX_PENDING_BASELINE is None:
            return

        jetzt = time.monotonic()
        if jetzt >= OUTBOX_PENDING_EXPIRES_AT:
            OUTBOX_PENDING_BASELINE = None
            OUTBOX_PENDING_EXPIRES_AT = 0.0
            OUTBOX_PENDING_NEXT_CHECK = 0.0
            return

        if jetzt < OUTBOX_PENDING_NEXT_CHECK:
            return

        OUTBOX_PENDING_NEXT_CHECK = jetzt + OUTBOX_PENDING_CHECK_INTERVAL

    try:
        with REFILL_SEND_LOCK:
            outbox_snapshot()
    except Exception:
        # Keep the alert and retry at the next bounded check interval.
        return


def warte_auf_outbox_bestaetigung(baseline):
    snapshot_fehler = False

    for wartezeit in OUTBOX_CONFIRMATION_DELAYS:
        time.sleep(wartezeit)
        try:
            snapshot = outbox_snapshot()
        except Exception:
            snapshot_fehler = True
            continue

        if snapshot - baseline:
            gui_status("letzter_refill", time.strftime("%H:%M:%S"))
            bereinige_fehler("outbox", nur_art="confirmation")
            return True

    merke_ausstehende_outbox_bestaetigung(baseline)
    meldung = (
        "Die Outbox konnte nach dem Refill nicht geprüft werden; "
        "die Bestätigung wird später erneut abgefragt."
        if snapshot_fehler
        else "Der Refill ist noch nicht in der Outbox bestätigt; "
        "die Bestätigung wird später erneut abgefragt."
    )
    melde_fehler("outbox", meldung, art="confirmation")
    return False


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

    pruefe_ausstehende_outbox_bestaetigung(snapshot)
    bereinige_fehler("outbox", nur_art="read")
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
        "Starte SMS-Reply..."
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
            "Reply-Formular nicht gefunden."
        )
        return False

    action = form.get(
        "action"
    )

    if not action:
        print(
            "Formular-Action nicht gefunden."
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
        print("Reply abgebrochen: Absendernummer im Detailformular ist nicht eindeutig 80808.")
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
        "Sende Reply an Cudy..."
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
            "Cudy hat den Reply "
            "nicht akzeptiert."
        )

        return False

    print()

    print(
        "Reply-Anfrage von Cudy angenommen; Outbox-Bestätigung steht noch aus."
    )

    return True


# ============================================================
# EINEN REFILL-VERSUCH
# ============================================================

def refill_versuch(cfg):
    print()
    print("Starte EINEN Refill-Versuch...")

    with REFILL_SEND_LOCK:
        try:
            outbox_vorher = outbox_snapshot()
        except PermissionError:
            raise
        except Exception as e:
            print("Outbox-Snapshot vor dem Refill fehlgeschlagen:", e)
            melde_fehler(
                "outbox",
                "Die Outbox konnte vor dem Refill nicht gelesen werden; der Versand wurde abgebrochen.",
                art="read",
            )
            raise OutboxSnapshotError from e

        try:
            erfolg = sende_reply(cfg, REFILL_TEXT)
        except PermissionError:
            raise
        except Exception:
            merke_ausstehende_outbox_bestaetigung(outbox_vorher)
            melde_fehler(
                "outbox",
                "Der Refill-Versand ist unklar; die Outbox wird weiter auf eine Bestätigung geprüft.",
                art="confirmation",
            )
            raise

        if not erfolg:
            melde_fehler(
                "outbox",
                "Refill-SMS wurde vom Router nicht als erfolgreich angenommen.",
                art="confirmation",
            )
            return False

        print("Warte auf Cudy-Verarbeitung...")
        print("Prüfe Outbox auf neue Refill-SMS...")
        if warte_auf_outbox_bestaetigung(outbox_vorher):
            print("Neues Refill in der Outbox bestätigt.")
            return True

        print("Refill ist noch nicht in der Outbox bestätigt.")
        return False

def router_session_sicherstellen():
    """Reuse the current login; renew it only when the router rejects it."""
    sms_page_url = f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms?iface=4g"
    response = session.get(sms_page_url, timeout=REQUEST_TIMEOUT)
    login_form = BeautifulSoup(response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    )

    if response.status_code == 403 or login_form:
        print("Router-Session abgelaufen; erneuere Login einmalig.")
        if not login():
            print("Manueller Refill fehlgeschlagen: Router-Login fehlgeschlagen.")
            return False
        response = session.get(sms_page_url, timeout=REQUEST_TIMEOUT)
        login_form = BeautifulSoup(response.text, "html.parser").find(
            "input", {"name": "luci_password"}
        )

    if response.status_code == 403 or login_form:
        print("Manueller Refill fehlgeschlagen: Router-Session nicht verfügbar.")
        melde_fehler("login", "Router-Sitzung konnte nicht wiederhergestellt werden.")
        return False

    response.raise_for_status()
    return True


def sende_neue_sms(empfaenger, nachricht):
    """Load and submit the Cudy SMS compose form directly."""
    form_url = (
        f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms/"
        "smsnew?nomodal=&iface=4g"
    )
    form_response = session.get(form_url, timeout=REQUEST_TIMEOUT)
    login_form = BeautifulSoup(form_response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    )
    if form_response.status_code == 403 or login_form:
        raise PermissionError("Router-Session ist vor dem SMS-Versand abgelaufen.")

    form_response.raise_for_status()
    form_soup = BeautifulSoup(form_response.text, "html.parser")
    form = None
    for kandidat in form_soup.find_all("form", {"name": "cbi"}):
        field_names = {
            feld.get("name")
            for feld in kandidat.find_all(["input", "textarea", "select"])
            if feld.get("name")
        }
        if (
            "cbid.smsnew.1.phone" in field_names
            and "cbid.smsnew.1.content" in field_names
        ):
            form = kandidat
            break

    if not form:
        print("smsnew-Seite enthält das erwartete Empfänger- und Nachrichtenfeld nicht.")
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
        print("Empfänger- und Textfelder im SMS-Formular sind nicht eindeutig erkennbar.")
        return False

    fields[phone_names[0]] = empfaenger
    fields[text_names[0]] = nachricht

    # The Cudy submit button is itself a named form field and must be included.
    send_button = form.find(
        ["button", "input"],
        {"name": "cbid.smsnew.1.send"}
    )
    if send_button:
        fields[send_button["name"]] = (
            send_button.get("value")
            or send_button.get_text(" ", strip=True)
            or "Send"
        )

    if "cbi.submit" in {item.get("name") for item in form.find_all("input")}:
        fields["cbi.submit"] = "1"
    if "timeclock" in fields:
        fields["timeclock"] = str(int(time.time()))

    action = form.get("action")
    if not action:
        print("Formularziel für neue SMS fehlt.")
        return False
    action_url = urljoin(form_response.url, action)
    headers = {**HEADERS, "Referer": form_response.url}
    result = session.post(action_url, data=fields, headers=headers, timeout=REQUEST_TIMEOUT)
    if result.status_code == 403:
        raise PermissionError("Cudy Session beim manuellen SMS-Versand abgelaufen.")
    if result.status_code not in (200, 302):
        print("Cudy hat die neue SMS nicht angenommen:", result.status_code)
        return False
    print("SMS wurde an den Router übermittelt.")
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
        "Gespeicherte SMS-States:",
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

        trigger_gefunden = ist_refill_trigger(sms["content"])

        if not trigger_gefunden:
            continue

        geschützte_sms.add(
            identifikation
        )

        print()

        print(
            "REFILL-SMS ERKANNT!"
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
            "   Read Count:",
            eintrag["read_count"]
        )

        print(
            "   Refill Attempts:",
            eintrag["refill_attempts"]
        )

        print(
            "   Refill Success:",
            eintrag["refill_success"]
        )

        if eintrag["refill_success"]:

            print(
                "   Refill bereits bestätigt."
            )

            continue

        if eintrag["read_count"] > 2:

            print(
                "   SMS wurde bereits "
                "mehrfach geprüft."
            )

            if eintrag["refill_attempts"] >= 2:

                print(
                    "   Maximale Refill-Versuche erreicht."
                )

            continue

        if eintrag["refill_attempts"] >= 2:

            print(
                "   Bereits 2 Refill-Versuche."
            )

            continue

        cfg = finde_cfg_fuer_sms(
            html,
            sms
        )

        if not cfg:

            print()

            print(
                "CFG NICHT GEFUNDEN!"
            )

            print(
                "   SMS bleibt offen "
                "und wird erneut geprüft."
            )

            continue

        print(
            "   SMS cfg:",
            cfg
        )

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

            erfolg = refill_versuch(cfg)

        except OutboxSnapshotError:

            eintrag[
                "refill_attempts"
            ] -= 1

            speichere_sms_state(
                state
            )

            continue

        except PermissionError:

            print()

            print(
                "Session während Refill abgelaufen."
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
                "FEHLER BEIM REFILL:",
                e
            )
            melde_fehler(
                "outbox",
                "Refill-Versand oder Outbox-Bestätigung ist fehlgeschlagen.",
                art="confirmation",
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
                "REFILL ERFOLGREICH!"
            )

        else:

            print()

            print(
                "REFILL NICHT BESTÄTIGT."
            )

            if (
                eintrag["read_count"] < 2
                and eintrag["refill_attempts"] < 2
            ):

                print(
                    "   Bei der nächsten "
                    "Prüfung erfolgt Versuch #2."
                )

            else:

                print(
                    "   Keine weiteren Versuche "
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
                "Löschen fehlgeschlagen:",
                response.status_code
            )

            return False

        print(
            "   Gelöscht:",
            cfg
        )

        return True

    except PermissionError:
        raise

    except Exception as e:

        print(
            "Löschfehler:",
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
        f"Prüfe "
        f"{'INBOX' if smsbox == 'rec' else 'OUTBOX'}..."
    )

    sms_liste = hole_alle_sms_mit_cfg(
        smsbox
    )

    anzahl = len(
        sms_liste
    )

    print(
        "   SMS:",
        anzahl
    )

    if smsbox == "rec":

        gui_status(
            "inbox",
            str(anzahl)
        )

    if anzahl < start_limit:

        print(
            f"   Unter Grenzwert "
            f"{start_limit}."
        )

        return

    zu_loeschen = (
        anzahl
        - target_limit
    )

    print(
        "   Speichergrenze erreicht."
    )

    print(
        f"   Ziel: "
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
                "   Geschützt:",
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
            "   Keine löschbaren SMS gefunden."
        )

        return

    gelöscht = 0

    for sms in kandidaten:

        print()

        print(
            "Cleanup:"
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
        f"Cleanup abgeschlossen. "
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
            "Session beim Cleanup abgelaufen."
        )

        raise

    except Exception as e:

        print(
            "Cleanup-Fehler:",
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
    bereinige_fehler("poll")

    gui_status(
        "letzter_poll",
        time.strftime("%H:%M:%S")
    )

    gui_status(
        "inbox",
        str(len(sms_liste))
    )

    print(
        f"Poll OK | "
        f"Inbox Seite 1: {len(sms_liste)} SMS"
    )

    geschützte_sms = verarbeite_sms_liste(
        sms_liste,
        html
    )

    pruefe_offene_outbox_bestaetigung()
    return geschützte_sms


# ============================================================
# SESSION RECOVERY
# ============================================================

def session_recovery():
    global letzter_session_refresh
    global letzter_erfolgreicher_poll
    global session_recovery_retry_at
    global session_recovery_retry_delay

    if time.monotonic() < session_recovery_retry_at:
        return False

    print()
    print("SESSION-RECOVERY...")
    gui_status("status", "Session-Recovery")
    session.cookies.clear()

    if login():
        session_recovery_retry_at = 0.0
        session_recovery_retry_delay = SESSION_RECOVERY_BASE_SECONDS
        letzter_session_refresh = time.monotonic()
        letzter_erfolgreicher_poll = time.monotonic()
        print("Session-Recovery erfolgreich.")
        gui_status("status", "Aktiv")
        return True

    print("Session-Recovery fehlgeschlagen.")
    print(
        f"Nächster Session-Recovery-Versuch in "
        f"{session_recovery_retry_delay} Sekunden."
    )
    session_recovery_retry_at = time.monotonic() + session_recovery_retry_delay
    session_recovery_retry_delay = min(
        session_recovery_retry_delay * 2,
        SESSION_RECOVERY_MAX_SECONDS,
    )
    gui_status("status", "Fehler")
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
        "CUDY AUTO REFILL V4.1"
    )

    print(
        "GUI MODUS"
    )

    print(
        "DAUERBETRIEB AKTIV"
    )

    print(
        f"Poll: "
        f"{POLL_INTERVAL}s"
    )

    print(
        f"Session-Refresh: "
        f"{('Aus' if SESSION_REFRESH_INTERVAL == 0 else f'{SESSION_REFRESH_INTERVAL / 60:g}min')}"
    )

    print(
        f"Watchdog: "
        f"{WATCHDOG_TIMEOUT}s"
    )

    print(
        "SMS-Prüfung: "
        "max. 2 Beobachtungen / Refill-Versuche"
    )

    print(
        "Refill-Trigger:"
    )

    for trigger in REFILL_TRIGGERS:
        print(
            f"   • {trigger}"
        )

    print(
        f"Inbox Cleanup: "
        f"{INBOX_CLEANUP_START} → "
        f"{INBOX_CLEANUP_TARGET}"
    )

    print(
        f"Outbox Cleanup: "
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

    login_retry_delay = LOGIN_RETRY_BASE_SECONDS
    while programm_laeuft:
        if login():
            break

        print()
        print("Login fehlgeschlagen.")
        print(
            f"Neuer Versuch in {login_retry_delay} Sekunden "
            f"(maximal {LOGIN_RETRY_MAX_SECONDS})."
        )
        for _ in range(login_retry_delay):
            if not programm_laeuft:
                return
            time.sleep(1)
        login_retry_delay = min(
            login_retry_delay * 2,
            LOGIN_RETRY_MAX_SECONDS,
        )

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
                    "Regelmäßiger "
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
                    "SESSION ABGELAUFEN!"
                )

                gui_status(
                    "status",
                    "Session abgelaufen"
                )
                melde_fehler(
                    "poll",
                    "Inbox-Polling wurde durch eine abgelaufene Router-Sitzung unterbrochen; Wiederherstellung läuft.",
                )

                if session_recovery():

                    poll_fehler = 0

                else:

                    poll_fehler += 1

            except requests.exceptions.Timeout:

                poll_fehler += 1

                print()

                print(
                    "REQUEST TIMEOUT!"
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
                    "Netzwerkfehler:",
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
                    "Poll-Fehler:",
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
            # ANHALTENDER INBOX-FEHLER
            # =================================================

            if poll_fehler >= MAX_POLL_FEHLER:
                melde_fehler(
                    "poll",
                    f"Inbox-Polling ist seit mindestens {MAX_POLL_FEHLER} Versuchen fehlgeschlagen; automatische Wiederherstellung läuft.",
                )

            # =================================================
            # 3 FEHLER IN FOLGE
            # =================================================

            if poll_fehler >= MAX_POLL_FEHLER:

                print()

                print(
                    "WATCHDOG RECOVERY"
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
                        "Recovery fehlgeschlagen."
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
                    "WATCHDOG!"
                )

                print(
                    f"   Seit "
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
                    "Cleanup benötigt "
                    "neue Session."
                )

                session_recovery()

            # =================================================
            # WARTEN
            # =================================================

            print()

            if POLL_INTERVAL <= 0:
                print(
                    "Automatischer Poll ist deaktiviert."
                )

                while programm_laeuft and POLL_INTERVAL <= 0:
                    time.sleep(1)

            else:
                print(
                    f"Nächster Check in "
                    f"{POLL_INTERVAL} Sekunden..."
                )

                for _ in range(POLL_INTERVAL):

                    if not programm_laeuft:
                        break

                    time.sleep(1)

        except Exception as e:

            print()

            print(
                "KRITISCHER FEHLER:",
                e
            )

            print(
                "Programm bleibt aktiv."
            )

            gui_status(
                "status",
                "Kritischer Fehler"
            )
            melde_fehler(
                "poll",
                "Ein unerwarteter Fehler ist aufgetreten; die Überwachung versucht fortzufahren.",
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

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1000, max(640, screen_width - 24))
        window_height = min(720, max(480, screen_height - 40))
        window_x = max(0, (screen_width - window_width) // 2)
        window_y = 0

        self.root.geometry(
            f"{window_width}x{window_height}+{window_x}+{window_y}"
        )

        self.root.minsize(
            min(850, max(640, screen_width - 24)),
            min(640, max(480, screen_height - 40))
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.schliessen
        )

        self.labels = {}
        self._band_test_running = False
        self._band_test_cancel = None
        self._band_dialog = None

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

        self.auto_follow = tk.BooleanVar(value=True)

        self.baue_gui()

        self.root.after(
            100,
            self.update_gui
        )

        self.root.after(
            500,
            self.erster_start_speedtest
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
            text="Cudy Auto Refill",
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
            text="Gestoppt",
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

        ttk.Style(self.root).configure(
            "CudyError.TLabel",
            foreground="#9c1c1c",
            padding=8,
        )
        self.error_banner = ttk.Label(
            self.root,
            text="",
            style="CudyError.TLabel",
            wraplength=920,
            justify="left",
            anchor="w",
        )
        self._error_banner_visible = False

        # ----------------------------------------------------
        # START / STOP
        # ----------------------------------------------------

        button_frame = ttk.Frame(
            self.root,
            padding=(15, 5, 15, 10)
        )

        button_frame.pack(
            side="bottom",
            fill="x"
        )

        self.start_button = ttk.Button(
            button_frame,
            text="Start",
            command=self.starten
        )

        self.start_button.pack(
            side="left",
            padx=(0, 5)
        )

        self.stop_button = ttk.Button(
            button_frame,
            text="Stop",
            command=self.stoppen,
            state="disabled"
        )

        self.stop_button.pack(
            side="left"
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
            text="Übersicht"
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
            text="SMS"
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
            text="Refill"
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
            text="Signal"
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
            text="Einstellungen"
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
            text="Log"
        )

        self.baue_log_tab()

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
            ("status", "Status"),
            ("router", "Router"),
            ("inbox", "Inbox"),
            ("letzter_refill", "Letzter Refill"),
            ("cleanup", "Cleanup"),
            ("watchdog", "Watchdog"),
            ("letzter_poll", "Letzter Poll"),
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
            text="System",
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
            text="SMS-Verwaltung",
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
            text="Inbox",
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
            text="Outbox",
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
            text="Überwachung",
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
            text="Aktualisieren",
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
            text="Refill",
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
            text="Letzter Refill",
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
            text="Refill-Konfiguration",
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
            text="Automatik",
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
            text="Manueller Notfall-Refill senden",
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
            text="Signal & Netzwerk",
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
        # LTE-BANDWAHL (ROUTERKONFIGURATION)
        # ----------------------------------------------------

        signal_frame = ttk.LabelFrame(
            tab,
            text="LTE-Bandwahl (Routerkonfiguration)",
            padding=20
        )

        signal_frame.pack(
            fill="x"
        )

        self.signal_value = ttk.Label(
            signal_frame,
            text="Noch nicht gelesen",
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
            text="Netzwerk",
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
        # LTE-BANDWAHL UND SPEEDTEST
        # ----------------------------------------------------

        band_frame = ttk.LabelFrame(tab, text="LTE-Bandvergleich", padding=15)
        band_frame.pack(fill="x", pady=(20, 0))

        ttk.Label(
            band_frame,
            text=(
                "Downloadtempo, Ping und Jitter werden je Band gemessen. "
                "Der Router verbindet sich dafür nacheinander mit jedem LTE-Band."
            ),
            wraplength=800
        ).pack(anchor="w")

        ttk.Label(
            band_frame,
            text=(
                "Datenverbrauch: bis zu etwa 80 MB bei zehn Bändern "
                "(zwei 4-MB-Downloads pro Band; Ping-Anfragen zusätzlich minimal)."
            ),
            wraplength=800
        ).pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(band_frame)
        buttons.pack(anchor="w", pady=(10, 0))

        self.signal_refresh_button = ttk.Button(
            buttons,
            text="Bandwahl lesen",
            command=self.signal_aktualisieren
        )
        self.signal_refresh_button.pack(side="left", padx=(0, 8))

        self.band_speedtest_button = ttk.Button(
            buttons,
            text="Alle LTE-Bänder testen",
            command=self.speedtest_alle_baender
        )
        self.band_speedtest_button.pack(side="left")

        self.band_speedtest_cancel_button = ttk.Button(
            buttons,
            text="Test abbrechen",
            command=self.speedtest_abbrechen,
            state="disabled"
        )
        self.band_speedtest_cancel_button.pack(side="left", padx=(8, 0))

        self.band_speedtest_status = tk.StringVar(
            value="Noch kein Bandvergleich durchgeführt."
        )
        ttk.Label(
            band_frame,
            textvariable=self.band_speedtest_status,
            wraplength=800
        ).pack(anchor="w", pady=(10, 0))

        self.band_speedtest_results = tk.Text(
            band_frame, height=8, wrap="word", state="disabled"
        )
        self.band_speedtest_results.pack(fill="x", pady=(8, 0))

        ttk.Label(
            tab,
            text=(
                "Bitte stoppe die SMS-Überwachung vor einem Bandtest. "
                "Du kannst den Vergleich später hier erneut starten."
            ),
            wraplength=800
        ).pack(anchor="w", pady=(15, 0))

    # ========================================================
    # EINSTELLUNGEN TAB
    # ========================================================

    def baue_einstellungen_tab(self):

        tab = self.einstellungen_tab

        ttk.Label(
            tab,
            text="Einstellungen",
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
            text="Router",
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
            text="SMS / Refill",
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
            text="Überwachung",
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
            text="LTE-Bänder",
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
            text="Bänder verwalten",
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
            text="Einstellungen speichern",
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
            text="Log löschen",
            command=self.log_loeschen
        )

        self.log_clear_button.pack(
            side="left"
        )
        self.log_copy_button = ttk.Button(
            button_frame,
            text="Diagnose kopieren",
            command=self.diagnose_kopieren,
        )
        self.log_copy_button.pack(side="left", padx=(8, 0))

        self.auto_follow_button = ttk.Checkbutton(
            button_frame,
            text="Auto-Follow: AN",
            variable=self.auto_follow,
            command=self.toggle_auto_follow,
        )
        self.auto_follow_button.pack(side="left", padx=(8, 0))

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
            "Stop angefordert..."
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
                "Programm muss laufen, "
                "um SMS zu aktualisieren."
            )

            return

        print()
        print(
            "Manuelle SMS-Aktualisierung..."
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
                    f"SMS-Aktualisierung: "
                    f"{len(sms_liste)} SMS"
                )

            except Exception as e:

                print(
                    "SMS-Aktualisierung fehlgeschlagen:",
                    e
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    # ========================================================
    # SIGNAL AKTUALISIEREN
    # ========================================================

    def erster_start_speedtest(self):
        if not getattr(sys, "frozen", False):
            return
        profile = lade_lte_profil()
        if profile.get("erststart_speedtest_gezeigt"):
            return
        profile["erststart_speedtest_gezeigt"] = True
        speichere_lte_profil(profile)
        if messagebox.askyesno(
            "LTE-Einrichtung",
            "Möchtest du jetzt alle verfügbaren LTE-Bänder vergleichen?\n\n"
            "Der Router verbindet sich für jedes Band neu. Bei zehn Bändern "
            "werden bis zu etwa 80 MB Mobilfunkdaten geladen.\n\n"
            "Du kannst den Test später im Reiter „Signal“ erneut starten.",
            parent=self.root
        ):
            self.speedtest_alle_baender(bestaetigung=False)

    def speedtest_abbrechen(self):
        if self._band_test_cancel is not None:
            self._band_test_cancel.set()
            self.band_speedtest_status.set(
                "Abbruch angefordert; die vorherige Bandwahl wird wiederhergestellt …"
            )

    def speedtest_alle_baender(self, bestaetigung=True):
        if self._band_test_running:
            return
        if programm_laeuft:
            messagebox.showinfo(
                "Überwachung anhalten",
                "Bitte stoppe zuerst die SMS-Überwachung. Der LTE-Test baut "
                "die Mobilfunkverbindung mehrfach neu auf.",
                parent=self.root
            )
            return
        if bestaetigung and not messagebox.askyesno(
            "Alle LTE-Bänder testen?",
            "Der Test schaltet nacheinander jedes verfügbare LTE-Band ein. "
            "Dabei wird die Verbindung mehrfach kurz getrennt.\n\n"
            "Bei zehn Bändern werden bis zu etwa 80 MB Mobilfunkdaten "
            "heruntergeladen. Die vorherige Bandwahl wird danach "
            "wiederhergestellt.\n\nJetzt starten?",
            parent=self.root
        ):
            return

        self._band_test_running = True
        self._band_test_cancel = threading.Event()
        self.band_speedtest_button.config(state="disabled")
        self.start_button.config(state="disabled")
        self.band_speedtest_cancel_button.config(state="normal")
        self.band_speedtest_status.set("Router-Bandeinstellungen werden gelesen …")
        self.band_speedtest_results.config(state="normal")
        self.band_speedtest_results.delete("1.0", "end")
        self.band_speedtest_results.config(state="disabled")

        def progress(message):
            try:
                self.root.after(
                    0, lambda message=message: self.band_speedtest_status.set(message)
                )
            except tk.TclError:
                pass

        def worker():
            outcome, error = None, None
            try:
                outcome = run_all_band_speedtest(
                    session, BASE_URL, router_session_sicherstellen,
                    progress=progress, cancel_event=self._band_test_cancel,
                )
                profile = lade_lte_profil()
                profile["letzter_bandtest"] = outcome
                speichere_lte_profil(profile)
                if outcome.get("restored"):
                    print("Vorherige LTE-Bandwahl wurde wiederhergestellt.")
                else:
                    print(
                        "Vorherige LTE-Bandwahl konnte nicht wiederhergestellt werden:",
                        outcome.get("restore_error")
                    )
            except Exception as exc:
                error = str(exc)
                print("LTE-Bandtest fehlgeschlagen:", exc)

            def finish():
                self._band_test_running = False
                self._band_test_cancel = None
                self.band_speedtest_button.config(state="normal")
                if not programm_laeuft:
                    self.start_button.config(state="normal")
                self.band_speedtest_cancel_button.config(state="disabled")
                if error:
                    self.band_speedtest_status.set("Bandvergleich fehlgeschlagen: " + error)
                    return
                if not outcome:
                    self.band_speedtest_status.set("Keine Messergebnisse vorhanden.")
                    return

                lines = []
                for item in outcome["results"]:
                    band = item["band"]
                    if item.get("download_mbps") is None:
                        lines.append(f"B{band}: {item.get('status', 'keine Messung')}")
                        continue
                    ping = item["ping_ms"]
                    jitter = item.get("jitter_ms")
                    jitter_text = f"{jitter:.1f} ms" if jitter is not None else "n/a"
                    lines.append(
                        f"B{band}: Download {item['download_mbps']:.1f} Mbit/s"
                        f" | Ping {ping:.0f} ms | Jitter {jitter_text}"
                        f" | Fehlversuche {item.get('loss_percent', 0):.0f}%"
                    )

                download_best = outcome.get("download_best")
                gaming_best = outcome.get("gaming_best")
                if download_best:
                    lines.append(
                        f"\nDownload-Empfehlung: B{download_best['band']} "
                        f"({download_best['download_mbps']:.1f} Mbit/s)"
                    )
                if gaming_best:
                    jitter = gaming_best.get("jitter_ms")
                    jitter_text = f"{jitter:.1f} ms" if jitter is not None else "n/a"
                    lines.append(
                        f"Gaming/Streaming nach Latenz: B{gaming_best['band']} "
                        f"(Ping {gaming_best['ping_ms']:.0f} ms, Jitter {jitter_text})"
                    )
                if outcome.get("error"):
                    lines.append("\nTest vorzeitig beendet: " + outcome["error"])
                if not outcome.get("restored"):
                    lines.append(
                        "\nACHTUNG: Vorherige Bandwahl bitte unter "
                        "„LTE-Bänder verwalten“ prüfen."
                    )

                self.band_speedtest_results.config(state="normal")
                self.band_speedtest_results.insert("1.0", "\n".join(lines))
                self.band_speedtest_results.config(state="disabled")
                self.band_speedtest_status.set(
                    "Test abgebrochen; Wiederherstellung wurde versucht."
                    if outcome.get("cancelled")
                    else (
                        "Bandvergleich mit Fehler beendet."
                        if outcome.get("error")
                        else "Bandvergleich abgeschlossen."
                    )
                )
                self.signal_aktualisieren()

            try:
                self.root.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ========================================================
    # SIGNAL AKTUALISIEREN
    # ========================================================

    def signal_aktualisieren(self):
        if programm_laeuft:
            return

        def worker():
            try:
                config = read_band_config(session, BASE_URL, router_session_sicherstellen)
                text = (
                    "Automatische Bandwahl"
                    if config["automatic"]
                    else "Manuell: " + ", ".join("B" + band for band in config["selected"])
                )
            except Exception as exc:
                text = "Bandwahl konnte nicht gelesen werden: " + str(exc)
            try:
                self.root.after(
                    0, lambda text=text: self.signal_value.config(text=text)
                )
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

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
            print("Manueller Refill gestartet: sende 'Refill' an 80808.")
            versand_gestartet = False
            outbox_vorher = None
            try:
                with REFILL_SEND_LOCK:
                    if not router_session_sicherstellen():
                        print("Manueller Refill fehlgeschlagen: keine gültige Router-Session.")
                        return

                    try:
                        vorher = outbox_snapshot()
                        outbox_vorher = vorher
                    except Exception:
                        melde_fehler(
                            "outbox",
                            "Die Outbox konnte vor dem manuellen Refill nicht gelesen werden; der Versand wurde abgebrochen.",
                            art="read",
                        )
                        print("Manueller Refill fehlgeschlagen: Outbox konnte nicht gelesen werden.")
                        return
                    bereinige_fehler("outbox", nur_art="read")

                    versand_gestartet = True
                    if not sende_neue_sms(LIDL_ABSENDER, REFILL_TEXT):
                        melde_fehler(
                            "outbox",
                            "Der manuelle Refill wurde vom Router nicht als erfolgreich angenommen.",
                            art="confirmation",
                        )
                        print("Manueller Refill fehlgeschlagen: SMS wurde nicht an den Router übermittelt.")
                        return

                    if not warte_auf_outbox_bestaetigung(vorher):
                        print("Manueller Refill noch nicht bestätigt; die Outbox wird weiter geprüft.")
                        return

                print("Manueller Refill erfolgreich: neue Refill-SMS in der Outbox bestätigt.")
            except Exception as exc:
                print("Manueller Refill fehlgeschlagen:", exc)
                if versand_gestartet:
                    if outbox_vorher is not None:
                        merke_ausstehende_outbox_bestaetigung(outbox_vorher)
                    melde_fehler(
                        "outbox",
                        "Der manuelle Refill konnte nicht sicher übermittelt oder bestätigt werden.",
                        art="confirmation",
                    )
                else:
                    melde_fehler(
                        "login",
                        "Die Router-Verbindung konnte für den manuellen Refill nicht geprüft werden.",
                    )
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
        if programm_laeuft or self._band_test_running:
            messagebox.showinfo(
                "Überwachung anhalten",
                "Bitte stoppe zuerst die SMS-Überwachung und laufende Bandtests.",
                parent=self.root
            )
            return

        fenster = tk.Toplevel(self.root)
        self._band_dialog = fenster
        fenster.title("LTE-Bänder")
        fenster.geometry("470x470")
        fenster.transient(self.root)
        fenster.grab_set()

        ttk.Label(
            fenster, text="LTE-Bandwahl", font=("Segoe UI", 16, "bold")
        ).pack(pady=(18, 8))
        status_var = tk.StringVar(value="Router-Bandeinstellungen werden geladen …")
        ttk.Label(
            fenster, textvariable=status_var, wraplength=420, justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 12))

        auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fenster, text="Automatische Bandwahl", variable=auto_var
        ).pack(anchor="w", padx=20)

        band_frame = ttk.LabelFrame(fenster, text="LTE-Bänder", padding=12)
        band_frame.pack(fill="x", padx=20, pady=10)
        band_vars = {band: tk.BooleanVar(value=False) for band in LTE_BANDS}
        band_widgets = {}
        for index, band in enumerate(LTE_BANDS):
            widget = ttk.Checkbutton(
                band_frame, text="B" + band, variable=band_vars[band]
            )
            widget.grid(row=index // 3, column=index % 3, sticky="w", padx=8, pady=5)
            band_widgets[band] = widget

        available_bands = set()

        def update_states(*_):
            for band, widget in band_widgets.items():
                state = "disabled" if auto_var.get() or band not in available_bands else "normal"
                widget.config(state=state)

        auto_var.trace_add("write", update_states)

        controls = ttk.Frame(fenster)
        controls.pack(side="bottom", fill="x", padx=20, pady=16)
        apply_button = ttk.Button(controls, text="Bandwahl anwenden", state="disabled")
        apply_button.pack(side="left")
        ttk.Button(controls, text="Schließen", command=fenster.destroy).pack(side="right")

        def finish_load(config=None, error=None):
            try:
                if not fenster.winfo_exists():
                    return
                if error:
                    status_var.set("Fehler: " + error)
                    return
                available_bands.update(config["available"])
                auto_var.set(config["automatic"])
                for band in LTE_BANDS:
                    band_vars[band].set(band in config["selected"])
                update_states()
                status_var.set(
                    "Wähle Automatik oder mindestens ein Band. "
                    "LTE verbindet sich nach der Änderung neu."
                )
                apply_button.config(state="normal")
            except tk.TclError:
                pass

        def load_worker():
            try:
                config = read_band_config(session, BASE_URL, router_session_sicherstellen)
                self.root.after(0, lambda config=config: finish_load(config=config))
            except Exception as exc:
                error = str(exc)
                try:
                    self.root.after(0, lambda error=error: finish_load(error=error))
                except tk.TclError:
                    pass

        def apply_selection():
            if programm_laeuft or self._band_test_running:
                messagebox.showinfo(
                    "Überwachung anhalten",
                    "Bitte stoppe zuerst die SMS-Überwachung.",
                    parent=fenster
                )
                return
            automatic = auto_var.get()
            selected = [band for band, var in band_vars.items() if var.get()]
            if not automatic and not selected:
                messagebox.showwarning(
                    "Band auswählen",
                    "Mindestens ein LTE-Band aktivieren oder Automatik wählen.",
                    parent=fenster
                )
                return
            if not messagebox.askyesno(
                "LTE-Bandwahl anwenden?",
                "Die Mobilfunkverbindung wird kurz getrennt und mit der "
                "neuen Bandwahl wieder aufgebaut.",
                parent=fenster
            ):
                return
            apply_button.config(state="disabled")
            status_var.set("Bandwahl wird gespeichert; LTE verbindet sich neu …")

            def apply_worker():
                outcome = {"status": "failed", "error": "Bandwechsel fehlgeschlagen."}

                def progress(message):
                    try:
                        self.root.after(
                            0,
                            lambda message=message: (
                                status_var.set(message) if fenster.winfo_exists() else None
                            ),
                        )
                    except tk.TclError:
                        pass

                try:
                    progress("Bandwahl wird angewendet …")
                    outcome = apply_band_config_with_fallback(
                        session, BASE_URL, router_session_sicherstellen,
                        automatic, selected, progress=progress, timeout=60,
                    )
                except Exception as exc:
                    outcome = {"status": "failed", "error": str(exc)}

                def finish_apply():
                    try:
                        if not fenster.winfo_exists():
                            return
                        kind = outcome["status"]
                        if kind == "applied":
                            fenster.destroy()
                            self.signal_aktualisieren()
                            print(
                                "LTE-Bandwahl gespeichert; Router und Internet "
                                "sind wieder erreichbar."
                            )
                        elif kind == "restored":
                            previous = outcome["previous"]
                            auto_var.set(previous["automatic"])
                            for band in LTE_BANDS:
                                band_vars[band].set(band in previous["selected"])
                            update_states()
                            status_var.set(
                                "Neue Bandwahl ohne Internet. Die vorherige Auswahl "
                                "wurde wiederhergestellt."
                            )
                            apply_button.config(state="normal")
                            print(
                                "LTE-Bandwahl zurückgerollt; vorherige Auswahl "
                                "und Internetverbindung wiederhergestellt."
                            )
                        elif kind == "restore_failed":
                            status_var.set(
                                "Bandwechsel fehlgeschlagen; auch die Wiederherstellung "
                                "konnte nicht bestätigt werden. Bitte Routerverbindung "
                                "und Bandwahl prüfen."
                            )
                            apply_button.config(state="normal")
                            print(
                                "LTE-Bandwechsel fehlgeschlagen:", outcome["error"],
                                "| Wiederherstellung:", outcome["restore_error"]
                            )
                        else:
                            status_var.set("Speichern fehlgeschlagen: " + outcome["error"])
                            apply_button.config(state="normal")
                    except tk.TclError:
                        pass

                try:
                    self.root.after(0, finish_apply)
                except tk.TclError:
                    pass

            threading.Thread(target=apply_worker, daemon=True).start()

        apply_button.config(command=apply_selection)
        threading.Thread(target=load_worker, daemon=True).start()

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

        BASE_URL = (self.router_entry.get().strip() or BASE_URL).rstrip("/")
        POLL_INTERVAL = neuer_poll
        SESSION_REFRESH_INTERVAL = neuer_session_refresh
        WATCHDOG_TIMEOUT = neuer_watchdog

        if not speichere_app_einstellungen({
            "router_url": BASE_URL,
            "poll_interval": POLL_INTERVAL,
            "session_refresh_interval": SESSION_REFRESH_INTERVAL,
            "watchdog_timeout": WATCHDOG_TIMEOUT,
        }):
            messagebox.showwarning(
                "Einstellungen nicht gespeichert",
                "Die Werte gelten in dieser Sitzung, konnten aber nicht für den "
                "nächsten Start gespeichert werden.",
                parent=self.root,
            )

        # Zeitgeber beim Speichern neu starten, damit die
        # neue Einstellung sofort und ohne alten Restwert greift.
        jetzt = time.monotonic()
        letzter_session_refresh = jetzt
        letzter_erfolgreicher_poll = jetzt

        print()
        print("Einstellungen gespeichert:")
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
    # DIAGNOSE KOPIEREN
    # ========================================================

    def diagnose_kopieren(self):
        try:
            with open(APP_LOG_FILE, "r", encoding="utf-8") as f:
                log_lines = f.readlines()[-250:]
        except OSError:
            log_lines = []
        safe_lines = [redact_log_line(line.rstrip()) for line in log_lines]
        report = [
            "Cudy Auto Refill – Diagnose",
            f"Python: {sys.version.split()[0]}",
            f"Plattform: {sys.platform}",
            "Status: " + ", ".join(
                f"{key}={value}" for key, value in status_data.items()
            ),
            "",
            "Letzte Protokollzeilen (SMS-Inhalte und Geheimnisse redigiert):",
            *safe_lines,
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(report))
        self.root.update_idletasks()
        messagebox.showinfo(
            "Diagnose kopiert",
            "Der redigierte Diagnosebericht ist in der Zwischenablage.",
            parent=self.root,
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
            "GUI-Log gelöscht."
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
                    line_count = int(self.log_text.index("end-1c").split(".")[0])
                    if line_count > MAX_GUI_LOG_LINES:
                        excess_lines = line_count - MAX_GUI_LOG_LINES
                        self.log_text.delete("1.0", f"{excess_lines + 1}.0")

                    if self.auto_follow.get():
                        self.log_text.see("end")

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

                    if key == "fehler":
                        self.error_banner.config(text=value)
                        if value and not self._error_banner_visible:
                            self.error_banner.pack(
                                before=self.notebook,
                                fill="x",
                                padx=15,
                                pady=(0, 8),
                            )
                            self._error_banner_visible = True
                        elif not value and self._error_banner_visible:
                            self.error_banner.pack_forget()
                            self._error_banner_visible = False

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
                text="Aktiv"
            )

        elif status in (
            "Fehler",
            "Login-Fehler",
            "Session abgelaufen",
            "Timeout",
            "Netzwerkfehler",
            "Poll-Fehler",
            "Kritischer Fehler"
        ):

            self.status_label.config(
                text="Fehler"
            )

        elif status == "Gestoppt":

            self.status_label.config(
                text="Gestoppt"
            )

        else:

            self.status_label.config(
                text=f"{status}"
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
                    text="Gestoppt"
                )

        self.root.after(
            100,
            self.update_gui
        )

    def toggle_auto_follow(self):
        label = "Auto-Follow: AN" if self.auto_follow.get() else "Auto-Follow: AUS"
        self.auto_follow_button.config(text=label)

    # ========================================================
    # SCHLIESSEN
    # ========================================================

    def schliessen(self):

        global programm_laeuft

        if programm_laeuft:

            programm_laeuft = False

            print()

            print(
                "Cudy Auto Refill wird beendet..."
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
