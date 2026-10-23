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
import uuid
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from lte_features import (
    LTE_BANDS,
    apply_band_config_with_fallback,
    recover_offline_manual_band,
    read_band_config,
    run_all_band_speedtest,
)

# ============================================================
# KONFIGURATION
# ============================================================

BASE_URL = "http://192.168.10.1"
FIRST_START_TEST_MODE = (
    os.environ.get("CUDY_AUTO_REFILL_FIRST_START_TEST") == "1"
)
FIRST_START_TEST_SCENARIO = "success"
FIRST_START_TEST_LOGIN_ATTEMPTS = 0
FIRST_START_TEST_POLL_DONE = False
FIRST_START_TEST_OUTBOX = set()
APP_HOME_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
LOCAL_DATA_ROOT = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
NORMAL_APP_DATA_DIR = os.path.join(LOCAL_DATA_ROOT, "CudyAutoRefill")
APP_DATA_DIR = os.path.join(
    LOCAL_DATA_ROOT,
    "CudyAutoRefill_FirstStartTest"
    if FIRST_START_TEST_MODE
    else "CudyAutoRefill",
)
CREDENTIALS_FILE = os.path.join(APP_DATA_DIR, "router_password.dpapi")
LEGACY_CREDENTIAL_FILES = (
    os.path.join(APP_DATA_DIR, "cudy_credentials.json"),
    os.path.join(APP_HOME_DIR, "cudy_credentials.json"),
)
SMS_STATE_FILE = os.path.join(APP_DATA_DIR, "sms_state.json")
LEGACY_SMS_STATE_FILE = os.path.join(APP_HOME_DIR, "sms_state.json")
if getattr(sys, "frozen", False):
    # Packaged builds must never depend on or migrate mutable files beside the
    # executable. Passwords and runtime data live only in the user's profile.
    LEGACY_CREDENTIAL_FILES = (os.path.join(APP_DATA_DIR, "cudy_credentials.json"),)
    LEGACY_SMS_STATE_FILE = None
if FIRST_START_TEST_MODE:
    # The test launcher must never import live credentials or SMS history.
    LEGACY_CREDENTIAL_FILES = ()
    LEGACY_SMS_STATE_FILE = os.path.join(
        APP_DATA_DIR,
        "__no_legacy_sms_state__.json",
    )
LTE_PROFILE_FILE = os.path.join(APP_DATA_DIR, "lte_profile.json")
APP_LOG_FILE = os.path.join(APP_DATA_DIR, "cudy_auto_refill.log")
SETTINGS_FILE = os.path.join(APP_DATA_DIR, "settings.json")
# Keep one lock shared by normal and test launches to prevent duplicate polling.
INSTANCE_LOCK_FILE = os.path.join(NORMAL_APP_DATA_DIR, "app.lock")
MAX_GUI_LOG_LINES = 3000

_instance_lock_handle = None


def acquire_single_instance_lock():
    """Hold an OS-level file lock until this process exits."""
    global _instance_lock_handle

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(INSTANCE_LOCK_FILE), exist_ok=True)
    lock_handle = open(INSTANCE_LOCK_FILE, "a+b")

    try:
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"\0")
            lock_handle.flush()
        lock_handle.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
    except OSError:
        lock_handle.close()
        return False
    except Exception:
        lock_handle.close()
        raise

    # Keeping this handle alive keeps the OS lock active. The OS releases it
    # automatically when the application process exits, including on crashes.
    _instance_lock_handle = lock_handle
    return True


def lade_app_einstellungen():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


APP_SETTINGS = {} if FIRST_START_TEST_MODE else lade_app_einstellungen()
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
    if FIRST_START_TEST_MODE:
        APP_SETTINGS.update(value)
        return True

    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        temporary = SETTINGS_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_FILE)
        APP_SETTINGS.update(value)
        return True
    except OSError as exc:
        print("Einstellungen konnten nicht gespeichert werden:", exc)
        return False


SETTINGS_EXPORT_FORMAT = "cudy-auto-refill-settings"
SETTINGS_EXPORT_VERSION = 1
SETTINGS_EXPORT_KEYS = (
    "router_url",
    "poll_interval",
    "session_refresh_interval",
    "watchdog_timeout",
)
SETTINGS_EXPORT_INTERVALS = {
    "poll_interval": {5, 10, 15, 30, 60, 120, 300},
    "session_refresh_interval": {0, 60, 120, 300, 600, 900, 1800, 3600},
    "watchdog_timeout": {0, 30, 45, 60, 90, 120, 180, 300},
}


def validiere_router_basis_url(wert):
    if not isinstance(wert, str) or not wert.strip() or len(wert) > 2048:
        raise ValueError("Die Router-Adresse fehlt oder ist zu lang.")

    try:
        parsed = urlsplit(wert.strip())
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("Die Router-Adresse ist ungültig.") from exc

    if (
        parsed.scheme.lower() not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Die Router-Adresse muss eine HTTP- oder HTTPS-Basisadresse ohne Zugangsdaten sein."
        )

    return f"{parsed.scheme.lower()}://{parsed.netloc}".rstrip("/")


def validiere_einstellungen_werte(einstellungen):
    if not isinstance(einstellungen, dict):
        raise ValueError("Die Einstellungsdaten haben ein ungültiges Format.")

    if set(einstellungen) != set(SETTINGS_EXPORT_KEYS):
        raise ValueError(
            "Die Datei enthält fehlende oder nicht unterstützte Einstellungen."
        )

    result = {
        "router_url": validiere_router_basis_url(einstellungen["router_url"]),
    }
    for name, erlaubte_werte in SETTINGS_EXPORT_INTERVALS.items():
        wert = einstellungen[name]
        if type(wert) is not int or wert not in erlaubte_werte:
            raise ValueError("Die Datei enthält ein ungültiges Überwachungsintervall.")
        result[name] = wert

    return result


def validiere_einstellungen_export(daten):
    if not isinstance(daten, dict) or set(daten) != {"format", "version", "settings"}:
        raise ValueError("Die Datei ist kein unterstützter Cudy-Einstellungsexport.")
    if daten.get("format") != SETTINGS_EXPORT_FORMAT:
        raise ValueError("Die Datei ist kein Cudy-Einstellungsexport.")
    if type(daten.get("version")) is not int or daten["version"] != SETTINGS_EXPORT_VERSION:
        raise ValueError("Diese Exportversion wird von der App nicht unterstützt.")
    return validiere_einstellungen_werte(daten.get("settings"))


def sekunden_aus_text(wert):
    if wert == "Aus":
        return 0

    try:
        zahl = int(wert.split()[0])
    except (AttributeError, IndexError, ValueError) as exc:
        raise ValueError("Ein Überwachungsintervall ist ungültig.") from exc

    if "Minute" in wert:
        return zahl * 60
    return zahl


def intervall_als_text(sekunden, einheit):
    if sekunden == 0:
        return "Aus"
    if einheit == "minuten":
        minuten = sekunden // 60
        return f"{minuten} Minute" if minuten == 1 else f"{minuten} Minuten"
    return f"{sekunden} Sekunden"


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
    if FIRST_START_TEST_MODE:
        raise RuntimeError("Im Erststart-Testmodus werden keine Passwörter gespeichert.")

    encrypted = _dpapi_transform(passwort.encode("utf-8"), protect=True)
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    temporary = CREDENTIALS_FILE + ".tmp"
    with open(temporary, "wb") as f:
        f.write(encrypted)
    os.replace(temporary, CREDENTIALS_FILE)


def lade_router_passwort():
    if FIRST_START_TEST_MODE:
        return ""

    try:
        with open(CREDENTIALS_FILE, "rb") as f:
            encrypted = f.read()
        return _dpapi_transform(encrypted, protect=False).decode("utf-8")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeDecodeError):
        print("Geschütztes Router-Passwort konnte nicht gelesen werden.")

    passwort = os.environ.get("CUDY_ROUTER_PASSWORD")
    if passwort:
        return passwort

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


def router_passwort_speicherstatus():
    if FIRST_START_TEST_MODE:
        return "Erststart-Test: Passwort wird bei jedem Start erneut abgefragt"
    if os.path.isfile(CREDENTIALS_FILE):
        return "In Windows DPAPI geschützt gespeichert"
    if os.environ.get("CUDY_ROUTER_PASSWORD"):
        return "Über Umgebungsvariable bereitgestellt"
    if any(os.path.isfile(path) for path in LEGACY_CREDENTIAL_FILES):
        return "Vorhanden; wird nach erfolgreichem Login geschützt übernommen"
    return "Noch nicht eingerichtet"


# The password is loaded lazily when the first router login is attempted.
PASSWORT = ""

def lade_lte_profil():
    if FIRST_START_TEST_MODE:
        return {}
    try:
        with open(LTE_PROFILE_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def speichere_lte_profil(value):
    if FIRST_START_TEST_MODE:
        return

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
ROUTER_AUTH_TIMEOUT = 20

# Watchdog
MAX_POLL_FEHLER = 3
WATCHDOG_TIMEOUT = _setting_int("watchdog_timeout", 45, 0, 86400)
LOGIN_RETRY_BASE_SECONDS = 10
LOGIN_RETRY_MAX_SECONDS = 120
SESSION_RECOVERY_BASE_SECONDS = 10
SESSION_RECOVERY_MAX_SECONDS = 120

# Download mode uses conservative, session-only monitoring values. Normal
# settings remain unchanged and take effect again as soon as the mode is off.
DOWNLOAD_MODE_POLL_INTERVAL = 5
DOWNLOAD_MODE_SESSION_REFRESH_INTERVAL = 60
DOWNLOAD_MODE_WATCHDOG_TIMEOUT = 30
DOWNLOAD_MODE_ACTIVE = False
DOWNLOAD_MODE_STARTED_AT = None
DOWNLOAD_PROACTIVE_ENABLED = False
DOWNLOAD_PROACTIVE_INTERVAL_SECONDS = 60
DOWNLOAD_PROACTIVE_MAX_RUNTIME_SECONDS = 12 * 60 * 60
DOWNLOAD_PROACTIVE_MAX_REFILLS = 20
DOWNLOAD_PROACTIVE_STARTED_AT = None
DOWNLOAD_PROACTIVE_NEXT_AT = None
DOWNLOAD_PROACTIVE_ATTEMPTS = 0
DOWNLOAD_PROACTIVE_IN_PROGRESS = False
DOWNLOAD_PROACTIVE_BLOCKED = False
DOWNLOAD_PROACTIVE_LAST_RESULT = "Noch kein Download-Modus gestartet."
DOWNLOAD_PROACTIVE_LAST_ACTION_ID = None
DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID = None
DOWNLOAD_LAST_REFILL_CONFIRMED_AT = None
DOWNLOAD_MODE_LOCK = threading.RLock()


def aktuelles_poll_intervall():
    return DOWNLOAD_MODE_POLL_INTERVAL if DOWNLOAD_MODE_ACTIVE else POLL_INTERVAL


def aktuelles_session_refresh_intervall():
    return (
        DOWNLOAD_MODE_SESSION_REFRESH_INTERVAL
        if DOWNLOAD_MODE_ACTIVE
        else SESSION_REFRESH_INTERVAL
    )


def aktueller_watchdog_timeout():
    return DOWNLOAD_MODE_WATCHDOG_TIMEOUT if DOWNLOAD_MODE_ACTIVE else WATCHDOG_TIMEOUT


def notiere_download_refill_versuch(aktions_id):
    """Delay the next proactive cycle after any other refill action."""
    global DOWNLOAD_PROACTIVE_NEXT_AT
    global DOWNLOAD_PROACTIVE_LAST_RESULT

    with DOWNLOAD_MODE_LOCK:
        if not DOWNLOAD_MODE_ACTIVE or not DOWNLOAD_PROACTIVE_ENABLED:
            return
        if aktions_id == DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID:
            return
        DOWNLOAD_PROACTIVE_NEXT_AT = (
            time.monotonic() + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
        )
        DOWNLOAD_PROACTIVE_LAST_RESULT = (
            "Andere Refill-Aktion läuft; nächster proaktiver Zyklus wartet das Intervall ab."
        )


def notiere_download_refill_bestaetigung(aktions_id, bestaetigt, abgelaufen=False):
    """Keep proactive sends behind the latest Outbox result."""
    global DOWNLOAD_PROACTIVE_BLOCKED
    global DOWNLOAD_PROACTIVE_NEXT_AT
    global DOWNLOAD_PROACTIVE_LAST_RESULT
    global DOWNLOAD_LAST_REFILL_CONFIRMED_AT

    with DOWNLOAD_MODE_LOCK:
        if not DOWNLOAD_MODE_ACTIVE:
            return
        if bestaetigt:
            DOWNLOAD_LAST_REFILL_CONFIRMED_AT = time.monotonic()
        if not DOWNLOAD_PROACTIVE_ENABLED:
            return
        if (
            DOWNLOAD_PROACTIVE_IN_PROGRESS
            and aktions_id == DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID
        ):
            return

        if bestaetigt:
            DOWNLOAD_PROACTIVE_BLOCKED = False
            DOWNLOAD_PROACTIVE_NEXT_AT = (
                time.monotonic() + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
            )
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Refill in der Outbox bestätigt; nächster Zyklus nach dem eingestellten Intervall."
            )
        elif abgelaufen or aktions_id == DOWNLOAD_PROACTIVE_LAST_ACTION_ID:
            DOWNLOAD_PROACTIVE_BLOCKED = True
            DOWNLOAD_PROACTIVE_NEXT_AT = None
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Refill nicht bestätigt; proaktive Zyklen pausiert. Outbox prüfen und Modus bewusst fortsetzen."
            )


def aktualisiere_download_laufzeitlimit():
    """Stop proactive sends once their configured session limit is reached."""
    global DOWNLOAD_PROACTIVE_ENABLED
    global DOWNLOAD_PROACTIVE_NEXT_AT
    global DOWNLOAD_PROACTIVE_LAST_RESULT

    meldung = None
    with DOWNLOAD_MODE_LOCK:
        if (
            DOWNLOAD_PROACTIVE_ENABLED
            and DOWNLOAD_PROACTIVE_STARTED_AT is not None
            and time.monotonic() - DOWNLOAD_PROACTIVE_STARTED_AT
            >= DOWNLOAD_PROACTIVE_MAX_RUNTIME_SECONDS
        ):
            DOWNLOAD_PROACTIVE_ENABLED = False
            DOWNLOAD_PROACTIVE_NEXT_AT = None
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Laufzeitlimit erreicht; proaktive Refill-Zyklen wurden gestoppt."
            )
            meldung = DOWNLOAD_PROACTIVE_LAST_RESULT

    if meldung:
        print("Download-Modus:", meldung)

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


def ist_lidl_absender(phone):
    """Accept Cudy's short-code value or a labelled form of the same sender."""
    normalized = normalisiere_sms_text(phone)
    if not normalized:
        return True

    digits = re.sub(r"\D", "", normalized)
    return (
        digits == LIDL_ABSENDER
        or normalized == "lidl connect"
        or re.search(r"(?<!\d)80808(?!\d)", normalized) is not None
    )


REFILL_TEXT = "Refill"
REFILL_SEND_LOCK = threading.Lock()


def ist_refill_outbox_text(text):
    """Accept the router's SMS label with repeated tooltip text, but no extra words."""
    tokens = re.findall(r"\w+", normalisiere_sms_text(text), flags=re.UNICODE)
    return bool(tokens) and all(token == REFILL_TEXT.casefold() for token in tokens)


def log_refill_aktion(aktions_id, schritt, details):
    """Write one redaction-safe, correlatable refill event to the app log."""
    print(f"Refill-Aktion [{aktions_id}] | {schritt} | {details}")

# ------------------------------------------------------------
# SMS CLEANUP
# ------------------------------------------------------------

INBOX_CLEANUP_START = 100
INBOX_CLEANUP_TARGET = 80

OUTBOX_CLEANUP_START = 50
OUTBOX_CLEANUP_TARGET = 40

DELETE_WAIT = 1
CLEANUP_INTERVAL_SECONDS = 300
CLEANUP_RETRY_SECONDS = 30


# ============================================================
# GUI / THREADING
# ============================================================

log_queue = queue.Queue()

programm_laeuft = False
backend_thread = None
ACTIVE_WORKER_THREADS = set()
ACTIVE_WORKER_THREADS_LOCK = threading.Lock()
naechster_cleanup_at = 0.0


def starte_hintergrund_thread(target, *, name=None):
    """Start a daemon worker and keep it tracked until its work has finished."""
    def tracked_target():
        try:
            target()
        finally:
            with ACTIVE_WORKER_THREADS_LOCK:
                ACTIVE_WORKER_THREADS.discard(threading.current_thread())

    worker = threading.Thread(target=tracked_target, name=name, daemon=True)
    with ACTIVE_WORKER_THREADS_LOCK:
        ACTIVE_WORKER_THREADS.add(worker)
    try:
        worker.start()
    except Exception:
        with ACTIVE_WORKER_THREADS_LOCK:
            ACTIVE_WORKER_THREADS.discard(worker)
        raise
    return worker


def aktive_hintergrund_threads():
    with ACTIVE_WORKER_THREADS_LOCK:
        return [worker for worker in ACTIVE_WORKER_THREADS if worker.is_alive()]

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
            if self.original_stdout is not None:
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
            if self.original_stdout is not None:
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

class NetzwerkGesperrteRouterSession:
    """Fail closed if the first-start test path attempts router I/O."""

    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = requests.structures.CaseInsensitiveDict()

    def request(self, method, url, *args, **kwargs):
        raise RuntimeError(
            "Router-Netzwerkzugriff ist im Erststart-Testmodus gesperrt."
        )

    def get(self, url, *args, **kwargs):
        return self.request("GET", url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self.request("POST", url, *args, **kwargs)

    def close(self):
        self.cookies.clear()


def erstelle_router_session():
    if FIRST_START_TEST_MODE:
        return NetzwerkGesperrteRouterSession()
    return requests.Session()


session = erstelle_router_session()

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
    if FIRST_START_TEST_MODE:
        raise RuntimeError("Router-Tokenabruf ist im Erststart-Testmodus gesperrt.")

    response = session.post(
        f"{BASE_URL}/cgi-bin/luci/admin/get_token",
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    token = response.text.strip()

    print("Router-Token erhalten.")

    return token


def pruefe_router_anmeldung(passwort, router_session=None):
    """Verify a candidate password by logging in and opening a protected page."""
    if FIRST_START_TEST_MODE:
        return (
            False,
            router_session or erstelle_router_session(),
            "Routeranmeldung ist im Erststart-Testmodus nur simuliert.",
        )

    router_session = router_session or erstelle_router_session()
    phase = "Router-Startseite"

    try:
        response = router_session.get(
            f"{BASE_URL}/cgi-bin/luci/",
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        html = response.text
        csrf = extrahiere_feld(html, "_csrf")
        salt = extrahiere_feld(html, "salt")
        username = extrahiere_feld(html, "luci_username") or "admin"

        phase = "Router-Token"
        token_response = router_session.post(
            f"{BASE_URL}/cgi-bin/luci/admin/get_token",
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        token_response.raise_for_status()
        token = token_response.text.strip()
        if not token:
            return False, router_session, "Der Router hat kein Login-Token geliefert."

        pass_hash = sha256(passwort + salt)
        pass_hash = sha256(pass_hash + token)
        phase = "Router-Login"
        login_response = router_session.post(
            f"{BASE_URL}/cgi-bin/luci/",
            data={
                "_csrf": csrf,
                "token": token,
                "salt": salt,
                "zonename": "Europe/Berlin",
                "timeclock": str(int(time.time())),
                "luci_language": "auto",
                "luci_username": username,
                "luci_password": pass_hash,
            },
            allow_redirects=False,
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        phase = "geschützte Routerseite"
        protected_page = router_session.get(
            f"{BASE_URL}/cgi-bin/luci/admin/network/gcom/sms?iface=4g",
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        login_form = BeautifulSoup(protected_page.text, "html.parser").find(
            "input", {"name": "luci_password"}
        )

        if protected_page.status_code == 200 and not login_form:
            return True, router_session, ""

        reason = (
            "Der Router zeigt weiterhin das Login-Formular."
            if login_form
            else f"Die geschützte Routerseite antwortet mit HTTP {protected_page.status_code}."
        )
        return False, router_session, reason
    except requests.Timeout:
        return False, router_session, f"Zeitüberschreitung beim Aufruf: {phase}."
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code is not None:
            return False, router_session, f"Der Router meldet bei {phase} HTTP {status_code}."
        return False, router_session, f"HTTP-Fehler beim Aufruf: {phase}."
    except requests.ConnectionError:
        return False, router_session, f"Die Verbindung zum Router brach bei {phase} ab."
    except requests.RequestException:
        return False, router_session, f"Netzwerkfehler beim Aufruf: {phase}."


def pruefe_und_speichere_router_passwort(passwort):
    """Store a replacement credential only after a fresh router login succeeds."""
    global PASSWORT
    global session

    if FIRST_START_TEST_MODE:
        if FIRST_START_TEST_SCENARIO == "password_rejected":
            return False, "Simulation: Das eingegebene Router-Passwort wurde abgelehnt."
        PASSWORT = passwort
        print("Testmodus: Passwort nur für diese Sitzung übernommen; nichts gespeichert.")
        return True, "Testmodus: Passwort nur simuliert und nicht gespeichert."

    router_session = erstelle_router_session()
    erfolgreich, router_session, grund = pruefe_router_anmeldung(
        passwort,
        router_session,
    )
    if not erfolgreich:
        return False, grund

    try:
        speichere_router_passwort(passwort)
    except Exception:
        return False, "Die Anmeldung war erfolgreich, aber Windows konnte das Passwort nicht sicher speichern."

    PASSWORT = passwort
    session = router_session
    return True, "Das Passwort wurde geprüft und sicher gespeichert."


def aendere_router_admin_passwort(altes_passwort, neues_passwort):
    """Change the Cudy admin password and persist it only after a fresh login."""
    global PASSWORT
    global session

    if FIRST_START_TEST_MODE:
        if FIRST_START_TEST_SCENARIO == "password_rejected":
            return False, "Simulation: Das bisherige Router-Passwort wurde abgelehnt."
        PASSWORT = neues_passwort
        print("Testmodus: Passwortänderung simuliert; der Router blieb unverändert.")
        return True, "Testmodus: Passwortänderung simuliert; nichts am Router geändert."

    router_session = erstelle_router_session()
    angemeldet, router_session, grund = pruefe_router_anmeldung(
        altes_passwort,
        router_session,
    )
    if not angemeldet:
        return False, "Das aktuelle Router-Passwort konnte nicht bestätigt werden. " + grund

    try:
        page = router_session.get(
            f"{BASE_URL}/cgi-bin/luci/admin/panel",
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        page.raise_for_status()
    except requests.RequestException:
        return False, "Die Router-Passwortseite konnte nicht geladen werden."

    soup = BeautifulSoup(page.text, "html.parser")
    form = soup.find("form", attrs={"name": "cbi"})
    if not form:
        return False, "Das Router-Passwortformular wurde auf der Einstellungsseite nicht gefunden."

    action = (form.get("action") or "").strip()
    if (
        form.get("method", "get").lower() != "post"
        or urljoin(BASE_URL + "/", action)
        != f"{BASE_URL}/cgi-bin/luci/admin/system/password"
    ):
        return False, "Das Router-Passwortformular entspricht nicht dem erwarteten Speicher-Endpunkt."

    form_values = {}
    for field in form.find_all("input"):
        field_name = field.get("name")
        if field_name:
            form_values[field_name] = field.get("value", "")

    required_fields = (
        "token",
        "cbid.luci._pass.pw1.salt",
        "cbid.luci._pass.pw2.salt",
        "cbid.luci._pass.pw3.salt",
    )
    if any(not form_values.get(field) for field in required_fields):
        return False, "Das Routerformular enthält nicht alle benötigten Sicherheitsfelder."

    passwort_felder = {
        "cbid.luci._pass.pw1": altes_passwort,
        "cbid.luci._pass.pw2": neues_passwort,
        "cbid.luci._pass.pw3": neues_passwort,
    }
    for field_name, value in passwort_felder.items():
        salt = form_values.get(field_name + ".salt", "")
        if not salt:
            return False, "Das Routerformular enthält keinen Salt für die Passwortprüfung."
        form_values[field_name] = value
        form_values[field_name + ".sha"] = sha256(value + salt)

    form_values["timeclock"] = str(int(time.time()))
    form_values["cbi.submit"] = "1"
    form_values["cbi.apply"] = ""
    action_url = urljoin(BASE_URL + "/", action)
    multipart_fields = {
        name: (None, str(value))
        for name, value in form_values.items()
    }

    try:
        response = router_session.post(
            action_url,
            files=multipart_fields,
            timeout=ROUTER_AUTH_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        return False, "Der Router hat die Passwortänderung nicht bestätigt. Der App-Speicher blieb unverändert."

    neue_session = erstelle_router_session()
    bestaetigt, neue_session, grund = pruefe_router_anmeldung(
        neues_passwort,
        neue_session,
    )
    if not bestaetigt:
        return False, (
            "Das Routerformular wurde abgesendet, aber die neue Anmeldung konnte nicht bestätigt werden. "
            "Das Router-Passwort kann bereits geändert worden sein; der App-Speicher wurde nicht überschrieben. "
            "Prüfe das neue Passwort im Router und nutze danach „Gespeichertes Passwort aktualisieren“. "
            + grund
        )

    try:
        speichere_router_passwort(neues_passwort)
    except Exception:
        PASSWORT = neues_passwort
        session = neue_session
        return False, (
            "Das Router-Passwort wurde geändert und die neue Anmeldung bestätigt, "
            "konnte aber nicht sicher gespeichert werden. Aktualisiere den App-Speicher, "
            "bevor du die Anwendung schließt."
        )

    PASSWORT = neues_passwort
    session = neue_session
    return True, "Das Router-Passwort wurde geändert, geprüft und sicher gespeichert."


def login():
    global PASSWORT
    global FIRST_START_TEST_LOGIN_ATTEMPTS
    print()
    print("Starte Cudy Login...")

    if FIRST_START_TEST_MODE:
        FIRST_START_TEST_LOGIN_ATTEMPTS += 1
        scenario = FIRST_START_TEST_SCENARIO
        fehlgeschlagen = (
            scenario == "router_offline"
            or (scenario == "retry_once" and FIRST_START_TEST_LOGIN_ATTEMPTS == 1)
            or scenario == "password_rejected"
        )
        if fehlgeschlagen:
            if scenario == "router_offline":
                grund = "Simulation: Router ist nicht erreichbar."
            elif scenario == "retry_once":
                grund = "Simulation: erster Loginversuch fehlgeschlagen."
            else:
                grund = "Simulation: Router-Passwort wurde abgelehnt."
            print("TESTMODUS – LOGIN FEHLGESCHLAGEN:", grund)
            gui_status("router", "Verbindung fehlgeschlagen (simuliert)")
            gui_status("status", "Login-Fehler")
            melde_fehler("login", grund + " Automatische Wiederholungen werden simuliert.")
            return False

        print("TESTMODUS – LOGIN ERFOLGREICH (simuliert; kein Router kontaktiert).")
        gui_status("router", "Verbunden (simuliert)")
        gui_status("status", "Aktiv")
        bereinige_fehler("login")
        return True

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
    if FIRST_START_TEST_MODE:
        raise RuntimeError("Router-Inbox-Zugriff ist im Erststart-Testmodus gesperrt.")

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


def collapse_repeated_text_halves(text):
    parts = str(text or "").split()
    while len(parts) >= 2 and len(parts) % 2 == 0:
        half = len(parts) // 2
        if parts[:half] != parts[half:]:
            break
        parts = parts[:half]
    return " ".join(parts)


def sms_cell_value(element):
    """Keep the original visible cell value for stable SMS-state IDs."""
    if not element:
        return ""
    paragraph = element.find("p")
    if paragraph:
        return paragraph.get_text(strip=True)

    text = sms_element_text(element)
    return collapse_repeated_text_halves(text)


def finde_sms_feldelement(row, *feldnamen):
    """Find Cudy SMS fields across both hyphenated and LuCI dotted IDs."""
    suffixes = tuple(
        re.compile(rf"(?:^|[._-]){re.escape(name)}$", re.IGNORECASE)
        for name in feldnamen
    )
    for element in row.find_all(attrs={"id": True}):
        element_id = str(element.get("id") or "")
        if any(pattern.search(element_id) for pattern in suffixes):
            return element
    return None


def extrahiere_sms_cfg_aus_row(row):
    """Read the detail-page CFG from the same Inbox row as the SMS fields."""
    button = row.find(
        "button",
        {
            "name": lambda value: value and value.endswith(".readsms")
        },
    )
    if not button:
        return None
    return extrahiere_cfg(button.get("onclick", ""))


def lese_sms(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    sms_liste = []

    rows = soup.find_all("tr")

    bereits_gefunden = set()

    for row_index, row in enumerate(rows, start=1):

        phone_element = finde_sms_feldelement(
            row, "phone", "sender", "number"
        )

        content_element = finde_sms_feldelement(
            row, "content", "message", "text"
        )

        timestamp_element = finde_sms_feldelement(
            row, "timestamp", "time", "date"
        )

        if not content_element:
            continue

        phone = sms_cell_value(phone_element)
        content = sms_element_text(content_element)
        timestamp = sms_cell_value(timestamp_element)

        # Some Cudy firmware revisions omit the timestamp field from a row.
        # Keep that SMS processable and use its stable row/field ID as identity.
        if not timestamp:
            timestamp = (
                str(row.get("data-timestamp") or row.get("data-id") or "")
                or str(row.get("id") or "")
                or str(content_element.get("id") or "")
                or f"row-{row_index}-{hashlib.sha256(str(row).encode('utf-8')).hexdigest()[:12]}"
            )

        if not content:
            continue

        sms = {
            "phone": phone,

            "content": content,

            "timestamp": timestamp,

            "cfg": extrahiere_sms_cfg_aus_row(row),
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
        parsed_rows = lese_sms(str(row))
        if not parsed_rows:
            continue
        sms = parsed_rows[0]
        identifikation = sms_id(sms)

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

    if FIRST_START_TEST_MODE:
        return {}

    if (
        not os.path.exists(SMS_STATE_FILE)
        and LEGACY_SMS_STATE_FILE
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

    if FIRST_START_TEST_MODE:
        return

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
OUTBOX_PENDING_ACTION_ID = None
OUTBOX_PENDING_SMS_STATE_ID = None
OUTBOX_PENDING_LOCK = threading.Lock()


class OutboxSnapshotError(Exception):
    """The Outbox baseline could not be read before attempting a refill."""


def merke_ausstehende_outbox_bestaetigung(
    baseline,
    aktions_id=None,
    sms_state_id=None,
):
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK
    global OUTBOX_PENDING_ACTION_ID
    global OUTBOX_PENDING_SMS_STATE_ID

    with OUTBOX_PENDING_LOCK:
        OUTBOX_PENDING_BASELINE = set(baseline)
        OUTBOX_PENDING_EXPIRES_AT = time.monotonic() + OUTBOX_PENDING_TIMEOUT
        OUTBOX_PENDING_NEXT_CHECK = time.monotonic() + OUTBOX_PENDING_CHECK_INTERVAL
        OUTBOX_PENDING_ACTION_ID = aktions_id
        OUTBOX_PENDING_SMS_STATE_ID = sms_state_id


def pruefe_ausstehende_outbox_bestaetigung(snapshot):
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK
    global OUTBOX_PENDING_ACTION_ID
    global OUTBOX_PENDING_SMS_STATE_ID

    with OUTBOX_PENDING_LOCK:
        if OUTBOX_PENDING_BASELINE is None:
            return False

        if time.monotonic() >= OUTBOX_PENDING_EXPIRES_AT:
            aktions_id = OUTBOX_PENDING_ACTION_ID
            sms_state_id = OUTBOX_PENDING_SMS_STATE_ID
            OUTBOX_PENDING_BASELINE = None
            OUTBOX_PENDING_EXPIRES_AT = 0.0
            OUTBOX_PENDING_NEXT_CHECK = 0.0
            OUTBOX_PENDING_ACTION_ID = None
            OUTBOX_PENDING_SMS_STATE_ID = None
            abgelaufen = True
            neue_eintraege = set()
        else:
            abgelaufen = False
            neue_eintraege = snapshot - OUTBOX_PENDING_BASELINE
            aktions_id = OUTBOX_PENDING_ACTION_ID
            sms_state_id = OUTBOX_PENDING_SMS_STATE_ID

        if not abgelaufen and neue_eintraege:
            OUTBOX_PENDING_BASELINE = None
            OUTBOX_PENDING_EXPIRES_AT = 0.0
            OUTBOX_PENDING_NEXT_CHECK = 0.0
            OUTBOX_PENDING_ACTION_ID = None
            OUTBOX_PENDING_SMS_STATE_ID = None

    if abgelaufen:
        if aktions_id:
            log_refill_aktion(
                aktions_id,
                "OUTBOX-BESTÄTIGUNG ABGELAUFEN",
                "Innerhalb des Prüfzeitraums wurde kein neuer Refill-Eintrag gefunden.",
            )
        notiere_download_refill_bestaetigung(
            aktions_id,
            False,
            abgelaufen=True,
        )
        return False

    if not neue_eintraege:
        if aktions_id:
            log_refill_aktion(
                aktions_id,
                "OUTBOX NOCH OFFEN",
                "Noch kein neuer Refill-Eintrag gefunden; Prüfung läuft weiter.",
            )
        else:
            print(
                "Outbox-Bestätigung offen: "
                f"passende Refill-Einträge={len(snapshot)}, neue=0."
            )
        return False

    if sms_state_id and not FIRST_START_TEST_MODE:
        try:
            state = lade_sms_state()
            eintrag = state.get(sms_state_id)
            if isinstance(eintrag, dict):
                eintrag["refill_success"] = True
                speichere_sms_state(state)
        except Exception as e:
            print("Refill-SMS-State konnte nach der Outbox-Bestätigung nicht aktualisiert werden:", e)

    gui_status("letzter_refill", time.strftime("%H:%M:%S"))
    bereinige_fehler("outbox", nur_art="confirmation")
    print("Verzögerte Refill-Bestätigung in der Outbox erkannt.")
    if aktions_id:
        log_refill_aktion(
            aktions_id,
            "OUTBOX-BESTÄTIGT",
            "Neuer Refill-Eintrag nach dem Versand gefunden.",
        )
    notiere_download_refill_bestaetigung(aktions_id, True)
    return True


def pruefe_offene_outbox_bestaetigung():
    global OUTBOX_PENDING_BASELINE
    global OUTBOX_PENDING_EXPIRES_AT
    global OUTBOX_PENDING_NEXT_CHECK
    global OUTBOX_PENDING_ACTION_ID
    global OUTBOX_PENDING_SMS_STATE_ID

    with OUTBOX_PENDING_LOCK:
        if OUTBOX_PENDING_BASELINE is None:
            return

        jetzt = time.monotonic()
        if jetzt >= OUTBOX_PENDING_EXPIRES_AT:
            aktions_id = OUTBOX_PENDING_ACTION_ID
            OUTBOX_PENDING_BASELINE = None
            OUTBOX_PENDING_EXPIRES_AT = 0.0
            OUTBOX_PENDING_NEXT_CHECK = 0.0
            OUTBOX_PENDING_ACTION_ID = None
            OUTBOX_PENDING_SMS_STATE_ID = None
            abgelaufen = True
        else:
            abgelaufen = False

        if not abgelaufen:
            if jetzt < OUTBOX_PENDING_NEXT_CHECK:
                return
            OUTBOX_PENDING_NEXT_CHECK = jetzt + OUTBOX_PENDING_CHECK_INTERVAL

    if abgelaufen:
        if aktions_id:
            log_refill_aktion(
                aktions_id,
                "OUTBOX-BESTÄTIGUNG ABGELAUFEN",
                "Innerhalb des Prüfzeitraums wurde kein neuer Refill-Eintrag gefunden.",
            )
        return

    try:
        with REFILL_SEND_LOCK:
            outbox_snapshot()
    except Exception:
        # Keep the alert and retry at the next bounded check interval.
        return


def warte_auf_outbox_bestaetigung(
    baseline,
    aktions_id=None,
    sms_state_id=None,
):
    snapshot_fehler = False

    if aktions_id:
        log_refill_aktion(
            aktions_id,
            "OUTBOX-PRÜFUNG",
            "Suche nach einem neuen Refill-Eintrag.",
        )

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
            if aktions_id:
                log_refill_aktion(
                    aktions_id,
                    "OUTBOX-BESTÄTIGT",
                    "Neuer Refill-Eintrag nach dem Versand gefunden.",
                )
            notiere_download_refill_bestaetigung(aktions_id, True)
            return True

    merke_ausstehende_outbox_bestaetigung(
        baseline,
        aktions_id,
        sms_state_id,
    )
    meldung = (
        "Die Outbox konnte nach dem Refill nicht geprüft werden; "
        "die Bestätigung wird später erneut abgefragt."
        if snapshot_fehler
        else "Der Refill ist noch nicht in der Outbox bestätigt; "
        "die Bestätigung wird später erneut abgefragt."
    )
    melde_fehler("outbox", meldung, art="confirmation")
    if aktions_id:
        log_refill_aktion(
            aktions_id,
            "OUTBOX NOCH OFFEN",
            "Bestätigung wird im Hintergrund weiter abgefragt.",
        )
    return False


def outbox_snapshot():

    if FIRST_START_TEST_MODE:
        return set(FIRST_START_TEST_OUTBOX)

    seiten = hole_alle_sms_seiten(
        "sto"
    )

    snapshot = set()

    for html in seiten.values():

        sms_liste = lese_sms(
            html
        )

        for sms in sms_liste:

            if normalisiere_sms_text(sms["phone"]) != normalisiere_sms_text(
                LIDL_ABSENDER
            ):
                continue

            if not ist_refill_outbox_text(sms["content"]):
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
    if FIRST_START_TEST_MODE:
        print("Testmodus: Reply-Versand blockiert; es wurde keine SMS gesendet.")
        return False

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

def refill_versuch(cfg, aktions_id, sms_state_id=None):
    if FIRST_START_TEST_MODE:
        baseline = outbox_snapshot()
        test_outbox_entry = f"SIMULIERTER-REFILL-{uuid.uuid4().hex[:8]}"
        FIRST_START_TEST_OUTBOX.add(test_outbox_entry)
        log_refill_aktion(
            aktions_id,
            "SIMULATION",
            "Refill-Anfrage simuliert; keine SMS wurde gesendet.",
        )
        if not (outbox_snapshot() - baseline):
            log_refill_aktion(
                aktions_id,
                "OUTBOX NICHT BESTÄTIGT",
                "Die simulierte Outbox enthält keinen neuen Refill-Eintrag.",
            )
            return False
        log_refill_aktion(
            aktions_id,
            "OUTBOX-BESTÄTIGT",
            "Neuer simulierter Refill-Eintrag wurde in der Test-Outbox gefunden.",
        )
        gui_status("letzter_refill", "Simuliert")
        print("Testmodus: Refill und Outbox-Bestätigung simuliert; kein Routerzugriff.")
        return True

    print()
    print("Starte EINEN Refill-Versuch...")
    log_refill_aktion(
        aktions_id,
        "VERSAND VORBEREITEN",
        "Lese den Outbox-Stand vor dem Reply.",
    )

    with REFILL_SEND_LOCK:
        try:
            outbox_vorher = outbox_snapshot()
        except PermissionError:
            raise
        except Exception as e:
            print("Outbox-Snapshot vor dem Refill fehlgeschlagen:", e)
            log_refill_aktion(
                aktions_id,
                "VERSAND ABGEBROCHEN",
                "Der Outbox-Ausgangsstand konnte nicht gelesen werden.",
            )
            melde_fehler(
                "outbox",
                "Die Outbox konnte vor dem Refill nicht gelesen werden; der Versand wurde abgebrochen.",
                art="read",
            )
            raise OutboxSnapshotError from e

        if sms_state_id:
            aktueller_state = lade_sms_state()
            aktueller_eintrag = aktueller_state.get(sms_state_id)
            if (
                isinstance(aktueller_eintrag, dict)
                and aktueller_eintrag.get("refill_success")
            ):
                log_refill_aktion(
                    aktions_id,
                    "VERSAND ÜBERSPRUNGEN",
                    "Der vorherige Versuch wurde inzwischen in der Outbox bestätigt.",
                )
                return True

        with OUTBOX_PENDING_LOCK:
            outbox_bestaetigung_offen = OUTBOX_PENDING_BASELINE is not None
        if outbox_bestaetigung_offen:
            log_refill_aktion(
                aktions_id,
                "VERSAND ABGEBROCHEN",
                "Eine vorherige Refill-SMS ist noch nicht in der Outbox bestätigt.",
            )
            raise OutboxSnapshotError(
                "Eine vorherige Refill-Aktion wartet noch auf Bestätigung."
            )

        with DOWNLOAD_MODE_LOCK:
            cooldown_rest = (
                DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
                - (time.monotonic() - DOWNLOAD_LAST_REFILL_CONFIRMED_AT)
                if (
                    DOWNLOAD_MODE_ACTIVE
                    and DOWNLOAD_LAST_REFILL_CONFIRMED_AT is not None
                )
                else 0
            )
        if cooldown_rest > 0:
            log_refill_aktion(
                aktions_id,
                "VERSAND VERSCHOBEN",
                f"Download-Mindestabstand läuft noch {int(cooldown_rest)} Sekunden.",
            )
            raise OutboxSnapshotError(
                "Der Mindestabstand nach der letzten bestätigten Refill-SMS läuft noch."
            )

        notiere_download_refill_versuch(aktions_id)

        try:
            log_refill_aktion(
                aktions_id,
                "VERSAND",
                "Sende die Reply-SMS über den Router.",
            )
            erfolg = sende_reply(cfg, REFILL_TEXT)
        except PermissionError:
            log_refill_aktion(
                aktions_id,
                "VERSAND ABGEBROCHEN",
                "Die Router-Sitzung ist abgelaufen.",
            )
            raise
        except Exception:
            merke_ausstehende_outbox_bestaetigung(
                outbox_vorher,
                aktions_id,
                sms_state_id,
            )
            log_refill_aktion(
                aktions_id,
                "VERSAND UNKLAR",
                "Die Router-Antwort war nicht eindeutig; Outbox-Prüfung läuft weiter.",
            )
            melde_fehler(
                "outbox",
                "Der Refill-Versand ist unklar; die Outbox wird weiter auf eine Bestätigung geprüft.",
                art="confirmation",
            )
            raise

        if not erfolg:
            log_refill_aktion(
                aktions_id,
                "VERSAND FEHLGESCHLAGEN",
                "Der Router hat die Reply-SMS nicht angenommen.",
            )
            melde_fehler(
                "outbox",
                "Refill-SMS wurde vom Router nicht als erfolgreich angenommen.",
                art="confirmation",
            )
            return False

        log_refill_aktion(
            aktions_id,
            "VERSAND ANGENOMMEN",
            "Der Router hat die Reply-Anfrage angenommen.",
        )
        print("Warte auf Cudy-Verarbeitung...")
        print("Prüfe Outbox auf neue Refill-SMS...")
        if warte_auf_outbox_bestaetigung(
            outbox_vorher,
            aktions_id,
            sms_state_id,
        ):
            print("Neues Refill in der Outbox bestätigt.")
            return True

        print("Refill ist noch nicht in der Outbox bestätigt.")
        return False

def router_session_sicherstellen():
    """Reuse the current login; renew it only when the router rejects it."""
    if FIRST_START_TEST_MODE:
        return False

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
    if FIRST_START_TEST_MODE:
        print("Testmodus: Manueller SMS-Versand blockiert; es wurde keine SMS gesendet.")
        return False

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
        # Reconcile states created by router markup that duplicated the sender
        # or timestamp in tooltip text, so a parser correction cannot reset a
        # previous attempt/success and send a second refill for the same SMS.
        for alte_identifikation, alter_eintrag in list(state.items()):
            if not isinstance(alter_eintrag, dict):
                continue
            if (
                alter_eintrag.get("content", "") == sms["content"]
                and collapse_repeated_text_halves(
                    alter_eintrag.get("phone", "")
                ) == sms["phone"]
                and collapse_repeated_text_halves(
                    alter_eintrag.get("timestamp", "")
                ) == sms["timestamp"]
            ):
                state.pop(alte_identifikation, None)
                state[identifikation] = alter_eintrag
                break

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

            "detail_checked":
                False,

            "detail_lookup_attempts":
                0,

            "detail_trigger_found":
                False,

            "trigger_observations":
                0,

            "created":
                time.time()
        }

    eintrag = state[
        identifikation
    ]

    # Older state files predate full-message lookup; keep those SMS eligible
    # for one safe detail-page check without resetting their refill history.
    eintrag.setdefault("detail_checked", False)
    eintrag.setdefault("detail_lookup_attempts", 0)
    eintrag.setdefault("detail_trigger_found", False)
    eintrag.setdefault("trigger_observations", 0)

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

        if not ist_lidl_absender(sms["phone"]):
            continue

        # ====================================================
        # NEUER REFILL-TRIGGER-POOL
        # ====================================================

        trigger_gefunden = (
            ist_refill_trigger(sms["content"])
            or eintrag.get("detail_trigger_found", False)
        )

        # Inbox rows can expose only a clipped preview. If that preview did
        # not match, read the full text from the detail page using the CFG
        # attached to this exact row. The poll itself remains page-1-only.
        if (
            not trigger_gefunden
            and not FIRST_START_TEST_MODE
            and not eintrag.get("detail_checked", False)
            and eintrag.get("detail_lookup_attempts", 0) < 3
        ):
            detail_cfg = sms.get("cfg") or finde_cfg_fuer_sms(html, sms)
            if detail_cfg:
                eintrag["detail_lookup_attempts"] += 1
                speichere_sms_state(state)
                try:
                    detail_sender, detail_content = hole_sms_detailinhalt(detail_cfg)
                except PermissionError:
                    raise
                except Exception:
                    detail_sender, detail_content = "", ""

                if detail_content:
                    eintrag["detail_checked"] = True
                    if (
                        detail_sender
                        and ist_lidl_absender(detail_sender)
                        and ist_refill_trigger(detail_content)
                    ):
                        eintrag["detail_trigger_found"] = True
                        trigger_gefunden = True
                elif eintrag["detail_lookup_attempts"] >= 3:
                    eintrag["detail_checked"] = True
                    print(
                        "SMS-Detailtext konnte nach drei Versuchen nicht gelesen werden; "
                        "für diese SMS wurde kein Refill ausgelöst."
                    )

                speichere_sms_state(state)

        if not trigger_gefunden:
            if eintrag["read_count"] == 1:
                print(
                    "Neue SMS in der Inbox erkannt; Text passt nicht zu einem Refill-Trigger."
                )
            continue

        aktions_id = identifikation[:8].upper()
        log_refill_aktion(
            aktions_id,
            "ERKANNT",
            "Eine passende Trigger-SMS wurde in der Inbox gefunden.",
        )

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

            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Kein Versand: diese Trigger-SMS ist bereits bestätigt.",
            )

            print(
                "   Refill bereits bestätigt."
            )

            continue

        if eintrag["refill_attempts"] >= 2:

            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Kein Versand: die maximale Zahl von zwei Versuchen ist erreicht.",
            )

            print(
                "   Bereits 2 Refill-Versuche."
            )

            continue

        cfg = (
            "SIMULIERT"
            if FIRST_START_TEST_MODE
            else sms.get("cfg") or finde_cfg_fuer_sms(html, sms)
        )

        if not cfg:

            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Noch kein Versand: Router-Zuordnung fehlt; SMS bleibt zur Prüfung offen.",
            )

            print()

            print(
                "CFG NICHT GEFUNDEN!"
            )

            print(
                "   SMS bleibt offen "
                "und wird erneut geprüft."
            )

            continue

        # read_count includes every inbox poll, including polls when the SMS
        # was not yet classified as a trigger. Only count eligible trigger
        # checks after the matching router CFG is available.
        eintrag["trigger_observations"] += 1
        speichere_sms_state(state)
        print(
            "   Trigger-Prüfungen:",
            eintrag["trigger_observations"]
        )

        if eintrag["trigger_observations"] > 2:
            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Kein weiterer Versuch: die Trigger-Prüfungsgrenze ist erreicht.",
            )
            print(
                "   SMS wurde bereits mehrfach als Refill-Trigger geprüft."
            )
            continue

        print(
            "   SMS cfg:",
            cfg
        )
        log_refill_aktion(
            aktions_id,
            "ENTSCHEIDUNG",
            f"Versand freigegeben; Versuch {eintrag['refill_attempts'] + 1} von 2.",
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

            erfolg = refill_versuch(
                cfg,
                aktions_id,
                identifikation,
            )

        except OutboxSnapshotError:

            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Versuch nicht gezählt; Outbox-Bestätigung oder Mindestabstand ließ keinen sicheren Versand zu.",
            )

            eintrag[
                "refill_attempts"
            ] -= 1

            speichere_sms_state(
                state
            )

            continue

        except PermissionError:

            log_refill_aktion(
                aktions_id,
                "ERGEBNIS",
                "Versand durch abgelaufene Router-Sitzung unterbrochen.",
            )

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

            log_refill_aktion(
                aktions_id,
                "ERGEBNIS",
                "Refill-Versand oder Outbox-Prüfung ist fehlgeschlagen.",
            )

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
                eintrag["trigger_observations"] < 2
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
    # Prefer the CFG extracted from the exact table row. Re-parsing the whole
    # table can produce a different fallback timestamp on some firmware.
    if sms.get("cfg"):
        return sms["cfg"]

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


def hole_sms_detailinhalt(cfg):
    """Fetch the full Inbox message from its Cudy detail form."""
    if not cfg:
        return "", ""

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
            "smslist?smsbox=rec&iface=4g"
        ),
    }

    response = session.get(
        details_url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 403:
        raise PermissionError("Cudy Session beim Lesen der SMS-Details abgelaufen.")
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form", {"name": "cbi"})
    if not form:
        return "", ""

    phone_field = form.find("input", {"name": "cbid.smsread.1.phone"})
    sender = phone_field.get("value", "").strip() if phone_field else ""

    content_field = form.find(
        ["input", "textarea"],
        {"name": "cbid.smsread.1.content"},
    )
    if not content_field:
        content_field = finde_sms_feldelement(form, "content", "message", "text")

    if not content_field:
        return sender, ""

    if content_field.name == "textarea":
        content = content_field.get_text(" ", strip=True)
    else:
        content = str(content_field.get("value") or "").strip()

    if not content:
        # Some firmware renders the received SMS as visible page text instead
        # of filling the reply form's content field.
        page_body = soup.body or soup
        content = page_body.get_text(" ", strip=True)

    # Never use a detail form with a missing or unrelated sender as evidence
    # for a refill trigger.
    if not sender or not ist_lidl_absender(sender):
        return sender, ""

    return sender, content


# ============================================================
# SMS LÖSCHEN
# ============================================================

def loesche_sms(
    cfg,
    smsbox
):
    if FIRST_START_TEST_MODE:
        print("Testmodus: SMS-Löschen blockiert; der Router bleibt unverändert.")
        return False

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
    if FIRST_START_TEST_MODE:
        print("Testmodus: SMS-Cleanup übersprungen; es wird nichts gelöscht.")
        return

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

    if FIRST_START_TEST_MODE:
        return True

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
        return True

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
        return False


def pruefe_download_refill_plan():
    """Start one bounded proactive cycle when no other refill is pending."""
    global DOWNLOAD_PROACTIVE_ATTEMPTS
    global DOWNLOAD_PROACTIVE_ENABLED
    global DOWNLOAD_PROACTIVE_IN_PROGRESS
    global DOWNLOAD_PROACTIVE_LAST_ACTION_ID
    global DOWNLOAD_PROACTIVE_LAST_RESULT
    global DOWNLOAD_PROACTIVE_NEXT_AT
    global DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID
    global DOWNLOAD_PROACTIVE_BLOCKED

    aktualisiere_download_laufzeitlimit()
    jetzt = time.monotonic()

    with DOWNLOAD_MODE_LOCK:
        if not DOWNLOAD_MODE_ACTIVE or not DOWNLOAD_PROACTIVE_ENABLED:
            return
        if DOWNLOAD_PROACTIVE_IN_PROGRESS or DOWNLOAD_PROACTIVE_BLOCKED:
            return
        if DOWNLOAD_PROACTIVE_ATTEMPTS >= DOWNLOAD_PROACTIVE_MAX_REFILLS:
            DOWNLOAD_PROACTIVE_ENABLED = False
            DOWNLOAD_PROACTIVE_NEXT_AT = None
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Maximale Zahl proaktiver Refills erreicht; weitere Zyklen gestoppt."
            )
            print("Download-Modus:", DOWNLOAD_PROACTIVE_LAST_RESULT)
            return
        if not programm_laeuft:
            return
        if DOWNLOAD_PROACTIVE_NEXT_AT is None:
            DOWNLOAD_PROACTIVE_NEXT_AT = (
                jetzt + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
            )
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Warte bis zum nächsten proaktiven Refill-Zyklus."
            )
            return
        if jetzt < DOWNLOAD_PROACTIVE_NEXT_AT:
            return

    with OUTBOX_PENDING_LOCK:
        outbox_offen = OUTBOX_PENDING_BASELINE is not None
    if outbox_offen:
        with DOWNLOAD_MODE_LOCK:
            if DOWNLOAD_MODE_ACTIVE and DOWNLOAD_PROACTIVE_ENABLED:
                DOWNLOAD_PROACTIVE_NEXT_AT = None
                DOWNLOAD_PROACTIVE_LAST_RESULT = (
                    "Warte auf die Bestätigung einer vorherigen Refill-Aktion."
                )
        return

    if not REFILL_SEND_LOCK.acquire(blocking=False):
        with DOWNLOAD_MODE_LOCK:
            if DOWNLOAD_MODE_ACTIVE and DOWNLOAD_PROACTIVE_ENABLED:
                DOWNLOAD_PROACTIVE_NEXT_AT = time.monotonic() + 10
                DOWNLOAD_PROACTIVE_LAST_RESULT = (
                    "Eine andere Refill-Aktion läuft; nächster Versuch nach kurzer Wartezeit."
                )
        return

    with OUTBOX_PENDING_LOCK:
        outbox_offen = OUTBOX_PENDING_BASELINE is not None
    with DOWNLOAD_MODE_LOCK:
        if (
            outbox_offen
            or not DOWNLOAD_MODE_ACTIVE
            or not DOWNLOAD_PROACTIVE_ENABLED
            or DOWNLOAD_PROACTIVE_IN_PROGRESS
            or DOWNLOAD_PROACTIVE_BLOCKED
            or DOWNLOAD_PROACTIVE_NEXT_AT is None
            or time.monotonic() < DOWNLOAD_PROACTIVE_NEXT_AT
        ):
            REFILL_SEND_LOCK.release()
            return

        DOWNLOAD_PROACTIVE_IN_PROGRESS = True
        DOWNLOAD_PROACTIVE_ATTEMPTS += 1
        aktions_id = f"DL-{uuid.uuid4().hex[:8].upper()}"
        DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID = aktions_id
        DOWNLOAD_PROACTIVE_LAST_ACTION_ID = aktions_id
        DOWNLOAD_PROACTIVE_NEXT_AT = None
        DOWNLOAD_PROACTIVE_LAST_RESULT = (
            f"Refill-Zyklus {DOWNLOAD_PROACTIVE_ATTEMPTS}/"
            f"{DOWNLOAD_PROACTIVE_MAX_REFILLS} läuft; Outbox-Bestätigung steht aus."
        )

    def worker():
        angenommen = False
        bestaetigt = False
        fehler = None
        try:
            if FIRST_START_TEST_MODE:
                bestaetigt = refill_versuch("SIMULIERT", aktions_id)
                angenommen = bestaetigt
            else:
                log_refill_aktion(
                    aktions_id,
                    "PROAKTIVER REFILL",
                    "Sende Refill nach dem konfigurierten Intervall ohne Inbox-Trigger.",
                )
                if not router_session_sicherstellen():
                    raise RuntimeError("Router-Sitzung konnte nicht hergestellt werden.")

                baseline = outbox_snapshot()
                log_refill_aktion(
                    aktions_id,
                    "VERSAND",
                    "Sende die proaktive Refill-SMS an 80808.",
                )
                if not sende_neue_sms(LIDL_ABSENDER, REFILL_TEXT):
                    raise RuntimeError("Der Router hat die SMS nicht angenommen.")

                angenommen = True
                log_refill_aktion(
                    aktions_id,
                    "VERSAND ANGENOMMEN",
                    "Der Router hat die proaktive SMS angenommen; prüfe die Outbox.",
                )
                bestaetigt = warte_auf_outbox_bestaetigung(baseline, aktions_id)
                if not bestaetigt:
                    fehler = "Noch keine neue Refill-SMS in der Outbox bestätigt."
        except Exception as exc:
            fehler = str(exc)
            log_refill_aktion(
                aktions_id,
                "PROAKTIVER REFILL FEHLGESCHLAGEN",
                "Versand oder Bestätigung fehlgeschlagen: " + fehler,
            )
            print("Download-Modus: proaktiver Refill fehlgeschlagen:", fehler)
        finally:
            REFILL_SEND_LOCK.release()
            with DOWNLOAD_MODE_LOCK:
                DOWNLOAD_PROACTIVE_IN_PROGRESS = False
                DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID = None
                if bestaetigt:
                    DOWNLOAD_PROACTIVE_BLOCKED = False
                    DOWNLOAD_PROACTIVE_LAST_RESULT = (
                        "Refill-SMS in der Outbox bestätigt."
                    )
                    if (
                        DOWNLOAD_MODE_ACTIVE
                        and DOWNLOAD_PROACTIVE_ENABLED
                        and DOWNLOAD_PROACTIVE_ATTEMPTS
                        >= DOWNLOAD_PROACTIVE_MAX_REFILLS
                    ):
                        DOWNLOAD_PROACTIVE_ENABLED = False
                        DOWNLOAD_PROACTIVE_NEXT_AT = None
                        DOWNLOAD_PROACTIVE_LAST_RESULT += (
                            " Maximale Zahl proaktiver Refills erreicht; Zyklen gestoppt."
                        )
                    elif DOWNLOAD_MODE_ACTIVE and DOWNLOAD_PROACTIVE_ENABLED:
                        DOWNLOAD_PROACTIVE_NEXT_AT = (
                            time.monotonic()
                            + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
                        )
                else:
                    DOWNLOAD_PROACTIVE_BLOCKED = True
                    DOWNLOAD_PROACTIVE_NEXT_AT = None
                    DOWNLOAD_PROACTIVE_LAST_RESULT = (
                        fehler
                        or (
                            "Refill nicht bestätigt; proaktive Zyklen pausiert."
                        )
                    )
                    if (
                        DOWNLOAD_PROACTIVE_ATTEMPTS
                        >= DOWNLOAD_PROACTIVE_MAX_REFILLS
                    ):
                        DOWNLOAD_PROACTIVE_ENABLED = False
                        DOWNLOAD_PROACTIVE_LAST_RESULT += (
                            " Maximale Zahl proaktiver Refills erreicht."
                        )
            if fehler:
                melde_fehler(
                    "outbox",
                    "Der proaktive Download-Refill wurde nicht sicher bestätigt. "
                    "Weitere automatische SMS sind bis zur Prüfung pausiert.",
                    art="confirmation",
                )

    try:
        starte_hintergrund_thread(worker, name="cudy-download-refill")
        print(
            "Download-Modus: proaktiver Refill gestartet "
            f"({DOWNLOAD_PROACTIVE_ATTEMPTS}/{DOWNLOAD_PROACTIVE_MAX_REFILLS})."
        )
    except Exception:
        REFILL_SEND_LOCK.release()
        with DOWNLOAD_MODE_LOCK:
            DOWNLOAD_PROACTIVE_IN_PROGRESS = False
            DOWNLOAD_PROACTIVE_CURRENT_ACTION_ID = None
            DOWNLOAD_PROACTIVE_ATTEMPTS = max(0, DOWNLOAD_PROACTIVE_ATTEMPTS - 1)
            DOWNLOAD_PROACTIVE_NEXT_AT = time.monotonic() + 10
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Der Refill-Hintergrundprozess konnte nicht gestartet werden."
            )


# ============================================================
# EIN POLL
# ============================================================

def poll():
    global letzter_erfolgreicher_poll

    if FIRST_START_TEST_MODE:
        global FIRST_START_TEST_POLL_DONE
        gui_status("letzter_poll", time.strftime("%H:%M:%S"))
        letzter_erfolgreicher_poll = time.monotonic()
        if FIRST_START_TEST_POLL_DONE:
            gui_status("inbox", "0 (simuliert)")
            print("Poll OK | Inbox Seite 1: 0 SMS (simuliert; kein Router kontaktiert).")
            return set()

        FIRST_START_TEST_POLL_DONE = True
        sms = {
            "phone": "",
            "content": "Deine Unlimited-1GB Option ist fast verbraucht.",
            "timestamp": "Erststart-Test",
        }
        print("Poll OK | 1 Beispiel-SMS ohne Absender simuliert.")
        gui_status("inbox", "1 (simuliert)")
        return verarbeite_sms_liste([sms], "")

    html = hole_sms_seite("rec", 1)
    sms_liste = lese_sms(html)

    if not sms_liste and BeautifulSoup(html, "html.parser").find("tr"):
        print(
            "SMS-Parser-Warnung: Inbox-Tabelle gefunden, aber keine SMS-Einträge erkannt."
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

    print(f"Poll OK | Inbox Seite 1: {len(sms_liste)} SMS")

    geschützte_sms = verarbeite_sms_liste(
        sms_liste,
        html,
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
    global programm_laeuft
    global letzter_session_refresh
    global letzter_erfolgreicher_poll
    global poll_fehler
    global naechster_cleanup_at

    # Cleanup runs on the first successful loop and then at a bounded interval.
    naechster_cleanup_at = 0.0

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
        f"{aktuelles_poll_intervall()}s"
    )

    print(
        f"Session-Refresh: "
        f"{('Aus' if aktuelles_session_refresh_intervall() == 0 else f'{aktuelles_session_refresh_intervall() / 60:g}min')}"
    )

    print(
        f"Watchdog: "
        f"{aktueller_watchdog_timeout()}s"
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
        f"Cleanup-Intervall: {CLEANUP_INTERVAL_SECONDS} Sekunden"
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

        if FIRST_START_TEST_MODE:
            if FIRST_START_TEST_LOGIN_ATTEMPTS >= 3:
                print("Testmodus: drei simulierte Loginversuche abgeschlossen.")
                gui_status("status", "Login-Fehler")
                programm_laeuft = False
                return
            print("Testmodus: simulierter Login-Retry in 1 Sekunde.")
            time.sleep(1)
            continue

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

    if not FIRST_START_TEST_MODE:
        try:
            band_recovery = recover_offline_manual_band(
                session,
                BASE_URL,
                router_session_sicherstellen,
                progress=print,
            )
            if band_recovery["status"] == "automatic_fallback":
                print(
                    "LTE-Startwiederherstellung: automatische Bandwahl wurde gespeichert. "
                    "Die SMS-Überwachung läuft weiter, während der Router die Verbindung aufbaut."
                )
            elif band_recovery["status"] == "manual_band_online":
                print("LTE-Startprüfung: manuelle Bandwahl hat Internetzugang.")
        except Exception as exc:
            print(
                "LTE-Startwiederherstellung fehlgeschlagen; "
                "die SMS-Überwachung wird trotzdem fortgesetzt:",
                exc,
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
                aktuelles_session_refresh_intervall() > 0
                and
                time.monotonic()
                - letzter_session_refresh
                >= aktuelles_session_refresh_intervall()
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
                aktueller_watchdog_timeout() > 0
                and
                zeit_seit_poll > aktueller_watchdog_timeout()
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

            if time.monotonic() >= naechster_cleanup_at:
                cleanup_erfolgreich = False
                try:
                    cleanup_erfolgreich = cleanup(geschützte_sms)
                except PermissionError:
                    print()
                    print("Cleanup benötigt neue Session.")
                    session_recovery()

                naechster_cleanup_at = time.monotonic() + (
                    CLEANUP_INTERVAL_SECONDS
                    if cleanup_erfolgreich
                    else CLEANUP_RETRY_SECONDS
                )

            pruefe_download_refill_plan()

            # =================================================
            # WARTEN
            # =================================================

            print()
            poll_intervall = aktuelles_poll_intervall()
            if poll_intervall <= 0:
                print("Automatischer Poll ist deaktiviert.")
            else:
                print(f"Nächster Check in {poll_intervall} Sekunden...")

            warte_start = time.monotonic()
            meldung_poll_aus = poll_intervall <= 0
            while programm_laeuft:
                poll_intervall = aktuelles_poll_intervall()
                if poll_intervall <= 0:
                    if not meldung_poll_aus:
                        print("Automatischer Poll ist deaktiviert.")
                        meldung_poll_aus = True
                    time.sleep(1)
                    continue
                if time.monotonic() - warte_start >= poll_intervall:
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
        if FIRST_START_TEST_MODE:
            self.root.title("Cudy Auto Refill — Erststart-Test")

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
        self._closing = False
        self._shutdown_wait_notice_written = False
        self._band_test_running = False
        self._band_test_cancel = None
        self._band_dialog = None
        self._router_passwort_dialog = None

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

        self.download_interval_var = tk.StringVar(value="60 Sekunden")
        self.download_duration_var = tk.StringVar(value="12 Stunden")
        self.download_max_refills_var = tk.StringVar(value="20")
        self.download_proactive_var = tk.BooleanVar(value=False)
        self.download_mode_status_var = tk.StringVar(
            value="Download-Modus ist ausgeschaltet."
        )
        self.download_next_cycle_var = tk.StringVar(value="Kein Zyklus geplant.")
        self.download_last_result_var = tk.StringVar(value="Noch kein Refill-Zyklus.")

        self.auto_follow = tk.BooleanVar(value=True)

        self.baue_gui()

        self.root.after(
            100,
            self.update_gui
        )

        self.root.after(
            500,
            self.start_first_start_flow
        )

    def start_first_start_flow(self):
        if not FIRST_START_TEST_MODE:
            self.erste_app_einrichtung()
            return

        fenster = tk.Toplevel(self.root)
        fenster.title("Erststart-Test: Szenario wählen")
        fenster.transient(self.root)
        fenster.resizable(False, False)
        fenster.protocol("WM_DELETE_WINDOW", lambda: starten())

        frame = ttk.Frame(fenster, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Sicherer Erststart-Test",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Alle Router-, LTE- und SMS-Aktionen bleiben simuliert. "
                "Wähle einen Ablauf; danach erscheint die normale Erststart-Einrichtung."
            ),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        scenario_var = tk.StringVar(value=FIRST_START_TEST_SCENARIO)
        scenarios = (
            ("success", "Verbindung erfolgreich"),
            ("retry_once", "Erster Login scheitert, Wiederholung klappt"),
            ("router_offline", "Router nicht erreichbar; drei Wiederholungen"),
            ("password_rejected", "Router-Passwort wird abgelehnt"),
        )
        for value, label in scenarios:
            ttk.Radiobutton(
                frame,
                text=label,
                value=value,
                variable=scenario_var,
            ).pack(anchor="w", pady=3)

        def starten():
            global FIRST_START_TEST_SCENARIO
            global FIRST_START_TEST_LOGIN_ATTEMPTS
            global FIRST_START_TEST_POLL_DONE
            global FIRST_START_TEST_OUTBOX

            FIRST_START_TEST_SCENARIO = scenario_var.get()
            FIRST_START_TEST_LOGIN_ATTEMPTS = 0
            FIRST_START_TEST_POLL_DONE = False
            FIRST_START_TEST_OUTBOX = set()
            try:
                fenster.destroy()
            except tk.TclError:
                pass
            print(
                "Erststart-Test-Szenario:",
                dict(scenarios).get(FIRST_START_TEST_SCENARIO, "Unbekannt"),
            )
            self.erste_app_einrichtung()

        ttk.Button(frame, text="Erststart durchspielen", command=starten).pack(
            anchor="e", pady=(14, 0)
        )
        fenster.bind("<Return>", lambda _event: starten())
        fenster.grab_set()
        fenster.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - fenster.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - fenster.winfo_height()) // 2
        fenster.geometry(f"+{max(0, x)}+{max(0, y)}")
        fenster.focus_set()

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
        # TAB 4 – DOWNLOAD
        # ====================================================

        self.download_tab = ttk.Frame(
            self.notebook,
            padding=15
        )

        self.notebook.add(
            self.download_tab,
            text="Download"
        )

        self.baue_download_tab()

        # ====================================================
        # TAB 5 – SIGNAL
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
        # TAB 6 – EINSTELLUNGEN
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
        # TAB 7 – LOG
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

    def baue_download_tab(self):
        tab = self.download_tab

        ttk.Label(
            tab,
            text="Download-Modus",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(
            tab,
            text=(
                "Für lange Downloads: häufigere SMS-Prüfung und auf Wunsch "
                "zeitlich begrenzte, proaktive Refill-SMS."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(0, 15))

        mode_frame = ttk.LabelFrame(tab, text="Sitzung", padding=15)
        mode_frame.pack(fill="x")

        self.download_mode_button = ttk.Button(
            mode_frame,
            text="Download-Modus einschalten",
            command=self.download_modus_umschalten,
        )
        self.download_mode_button.pack(anchor="w")

        ttk.Label(
            mode_frame,
            textvariable=self.download_mode_status_var,
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Label(
            mode_frame,
            text=(
                "Aktives Profil: Inbox-Prüfung alle 5 Sekunden, "
                "Session-Refresh alle 60 Sekunden, Watchdog nach 30 Sekunden. "
                "Beim Ausschalten gelten wieder deine normalen Einstellungen."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        proactive_frame = ttk.LabelFrame(
            tab,
            text="Proaktive Refill-Zyklen",
            padding=15,
        )
        proactive_frame.pack(fill="x", pady=(15, 0))

        self.download_proactive_check = ttk.Checkbutton(
            proactive_frame,
            text="Proaktive Refill-SMS ohne Inbox-Trigger erlauben",
            variable=self.download_proactive_var,
            command=self.download_proaktiv_umschalten,
        )
        self.download_proactive_check.pack(anchor="w")

        options = ttk.Frame(proactive_frame)
        options.pack(anchor="w", fill="x", pady=(10, 0))

        ttk.Label(options, text="Mindestintervall:").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.download_interval_combo = ttk.Combobox(
            options,
            textvariable=self.download_interval_var,
            values=("60 Sekunden", "90 Sekunden", "2 Minuten", "5 Minuten", "10 Minuten"),
            state="readonly",
            width=16,
        )
        self.download_interval_combo.grid(row=0, column=1, sticky="w")
        self.download_interval_combo.bind(
            "<<ComboboxSelected>>", self.download_einstellungen_aktualisieren
        )

        ttk.Label(options, text="Maximale Laufzeit:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.download_duration_combo = ttk.Combobox(
            options,
            textvariable=self.download_duration_var,
            values=("1 Stunde", "2 Stunden", "4 Stunden", "8 Stunden", "12 Stunden"),
            state="readonly",
            width=16,
        )
        self.download_duration_combo.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.download_duration_combo.bind(
            "<<ComboboxSelected>>", self.download_einstellungen_aktualisieren
        )

        ttk.Label(options, text="Maximale Refill-SMS:").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.download_max_refills_combo = ttk.Combobox(
            options,
            textvariable=self.download_max_refills_var,
            values=("1", "2", "5", "10", "20", "40"),
            state="readonly",
            width=16,
        )
        self.download_max_refills_combo.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.download_max_refills_combo.bind(
            "<<ComboboxSelected>>", self.download_einstellungen_aktualisieren
        )

        status_frame = ttk.LabelFrame(tab, text="Status", padding=15)
        status_frame.pack(fill="x", pady=(15, 0))

        ttk.Label(status_frame, textvariable=self.download_next_cycle_var).pack(
            anchor="w"
        )
        ttk.Label(
            status_frame,
            textvariable=self.download_last_result_var,
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        ttk.Label(
            tab,
            text=(
                "Proaktive Refill-SMS sind standardmäßig ausgeschaltet. "
                "Die App bestätigt nur, dass die SMS in der Router-Outbox erscheint; "
                "sie kann die tatsächliche Freischaltung des Datenvolumens nicht bestätigen."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(12, 0))

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

        password_frame = ttk.LabelFrame(
            router_frame,
            text="Admin-Passwort",
            padding=10,
        )
        password_frame.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(10, 0),
        )

        self.router_password_status_label = ttk.Label(
            password_frame,
            text=router_passwort_speicherstatus(),
        )
        self.router_password_status_label.pack(anchor="w")

        ttk.Label(
            password_frame,
            text=(
                "Du kannst das Router-Passwort hier ändern oder ein bereits "
                "geändertes Passwort für die App bestätigen."
            ),
            wraplength=760,
        ).pack(anchor="w", pady=(4, 8))

        password_buttons = ttk.Frame(password_frame)
        password_buttons.pack(anchor="w")

        ttk.Button(
            password_buttons,
            text="Router-Passwort ändern",
            command=lambda: self.router_passwort_dialog("change"),
        ).pack(side="left")

        ttk.Button(
            password_buttons,
            text="Gespeichertes Passwort aktualisieren",
            command=lambda: self.router_passwort_dialog("update"),
        ).pack(side="left", padx=(8, 0))

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
        # EINSTELLUNGEN ÜBERTRAGEN
        # ----------------------------------------------------

        settings_transfer_frame = ttk.LabelFrame(
            tab,
            text="Einstellungen übertragen",
            padding=15,
        )
        settings_transfer_frame.pack(
            fill="x",
            pady=(15, 0),
        )

        ttk.Label(
            settings_transfer_frame,
            text=(
                "Der Export enthält nur Router-Adresse und Überwachungsintervalle. "
                "Passwörter, Tokens, SMS-Inhalte und SMS-Verlauf werden nicht übertragen."
            ),
            wraplength=800,
        ).pack(anchor="w")

        settings_transfer_buttons = ttk.Frame(settings_transfer_frame)
        settings_transfer_buttons.pack(anchor="w", pady=(8, 0))

        ttk.Button(
            settings_transfer_buttons,
            text="Einstellungen exportieren",
            command=self.einstellungen_exportieren,
        ).pack(side="left")

        ttk.Button(
            settings_transfer_buttons,
            text="Einstellungen importieren",
            command=self.einstellungen_importieren,
        ).pack(side="left", padx=(8, 0))

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

        global PASSWORT
        global programm_laeuft
        global backend_thread
        global FIRST_START_TEST_LOGIN_ATTEMPTS
        global FIRST_START_TEST_POLL_DONE
        global FIRST_START_TEST_OUTBOX

        if programm_laeuft:
            return

        if aktive_hintergrund_threads():
            print(
                "Start wartet, bis vorherige Hintergrundaktionen beendet sind."
            )
            return

        if FIRST_START_TEST_MODE:
            FIRST_START_TEST_LOGIN_ATTEMPTS = 0
            FIRST_START_TEST_POLL_DONE = False
            FIRST_START_TEST_OUTBOX = set()

        if not PASSWORT:
            PASSWORT = lade_router_passwort()
        if not PASSWORT:
            self.router_passwort_dialog(
                "update",
                on_success=self.starten,
            )
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

        try:
            backend_thread = starte_hintergrund_thread(
                hauptschleife,
                name="cudy-backend",
            )
        except Exception as exc:
            programm_laeuft = False
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            gui_status("status", "Gestoppt")
            print("Backend-Thread konnte nicht gestartet werden:", exc)

    # ========================================================
    # ROUTER-PASSWORT
    # ========================================================

    def router_passwort_dialog(self, modus, on_success=None, on_close=None):
        if self._router_passwort_dialog is not None:
            try:
                self._router_passwort_dialog.lift()
                self._router_passwort_dialog.focus_force()
            except tk.TclError:
                pass
            return

        if programm_laeuft or self._band_test_running:
            messagebox.showinfo(
                "Router-Passwort",
                "Bitte stoppe zuerst die Überwachung und laufende LTE-Tests.",
                parent=self.root,
            )
            return

        if modus not in ("change", "update"):
            raise ValueError("Unbekannter Router-Passwortdialog.")

        dialog = tk.Toplevel(self.root)
        self._router_passwort_dialog = dialog
        dialog.title(
            "Router-Passwort ändern"
            if modus == "change"
            else "Router-Passwort für die App bestätigen"
        )
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_dialog())

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)

        if modus == "change":
            title_text = "Router-Admin-Passwort ändern"
            help_text = (
                "Die Änderung wird direkt am Router gespeichert. "
                "Danach prüft die App die neue Anmeldung und speichert "
                "das Passwort geschützt für dein Windows-Benutzerkonto."
            )
            fields = [
                ("Aktuelles Passwort", "old"),
                ("Neues Passwort (8–64 Zeichen)", "new"),
                ("Neues Passwort bestätigen", "confirm"),
            ]
        else:
            title_text = "Router-Passwort für die App bestätigen"
            help_text = (
                "Gib das aktuelle Admin-Passwort ein. Die App prüft die "
                "Anmeldung am Router und speichert es erst danach geschützt."
            )
            fields = [("Aktuelles Router-Passwort", "current")]

        if FIRST_START_TEST_MODE:
            help_text = (
                "Sicherer Test: Die Eingabe wird nur in dieser Sitzung verwendet. "
                "Es wird kein Router kontaktiert und kein Passwort gespeichert."
            )

        ttk.Label(
            frame,
            text=title_text,
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text=help_text,
            wraplength=460,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 14))

        entries = {}
        for row, (label_text, key) in enumerate(fields, start=2):
            ttk.Label(frame, text=label_text).grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 12)
            )
            entry = ttk.Entry(frame, show="•", width=34)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entries[key] = entry

        if modus == "change":
            gespeichertes_passwort = PASSWORT or lade_router_passwort()
            if gespeichertes_passwort:
                entries["old"].insert(0, gespeichertes_passwort)

        status = tk.StringVar(value="")
        ttk.Label(
            frame,
            textvariable=status,
            wraplength=460,
        ).grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="e", pady=(14, 0))

        submit_button = ttk.Button(
            buttons,
            text="Passwort ändern" if modus == "change" else "Prüfen und speichern",
        )
        submit_button.pack(side="right")
        ttk.Button(
            buttons,
            text="Abbrechen",
            command=lambda: close_dialog(),
        ).pack(side="right", padx=(0, 8))

        closed = False
        busy = False

        def close_dialog():
            nonlocal closed
            if closed or busy:
                return
            closed = True
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self._router_passwort_dialog = None
            if on_close:
                on_close()

        def submit():
            nonlocal busy
            if busy:
                return

            if modus == "change":
                old_password = entries["old"].get()
                new_password = entries["new"].get()
                confirm_password = entries["confirm"].get()
                if not old_password:
                    messagebox.showwarning(
                        "Passwort fehlt",
                        "Bitte gib das aktuelle Router-Passwort ein.",
                        parent=dialog,
                    )
                    return
                if not 8 <= len(new_password) <= 64:
                    messagebox.showwarning(
                        "Ungültiges Passwort",
                        "Das neue Router-Passwort muss 8 bis 64 Zeichen lang sein.",
                        parent=dialog,
                    )
                    return
                if new_password != confirm_password:
                    messagebox.showwarning(
                        "Passwörter stimmen nicht überein",
                        "Bitte wiederhole das neue Passwort identisch.",
                        parent=dialog,
                    )
                    return
                if new_password == old_password:
                    messagebox.showwarning(
                        "Keine Änderung",
                        "Das neue Passwort muss sich vom aktuellen Passwort unterscheiden.",
                        parent=dialog,
                    )
                    return
                task = lambda: aendere_router_admin_passwort(
                    old_password,
                    new_password,
                )
            else:
                current_password = entries["current"].get()
                if not current_password:
                    messagebox.showwarning(
                        "Passwort fehlt",
                        "Bitte gib das aktuelle Router-Passwort ein.",
                        parent=dialog,
                    )
                    return
                task = lambda: pruefe_und_speichere_router_passwort(
                    current_password,
                )

            busy = True
            status.set("Router-Anmeldung wird geprüft …")
            submit_button.config(state="disabled")
            for entry in entries.values():
                entry.config(state="disabled")

            def worker():
                try:
                    outcome = task()
                except Exception:
                    outcome = (
                        False,
                        "Der Router-Vorgang ist unerwartet fehlgeschlagen. "
                        "Prüfe die Routerverbindung und versuche es erneut.",
                    )

                def finish():
                    nonlocal busy
                    if closed:
                        return
                    busy = False
                    success, message = outcome
                    if success:
                        status.set(message)
                        if hasattr(self, "router_password_status_label"):
                            self.router_password_status_label.config(
                                text=router_passwort_speicherstatus()
                            )
                        if on_success:
                            on_success()
                        messagebox.showinfo(
                            "Router-Passwort",
                            message,
                            parent=dialog,
                        )
                        close_dialog()
                    else:
                        status.set(message)
                        submit_button.config(state="normal")
                        for entry in entries.values():
                            entry.config(state="normal")
                        messagebox.showerror(
                            "Router-Passwort nicht bestätigt",
                            message,
                            parent=dialog,
                        )

                try:
                    self.root.after(0, finish)
                except tk.TclError:
                    pass

            starte_hintergrund_thread(worker, name="cudy-password-update")

        submit_button.config(command=submit)
        dialog.bind("<Return>", lambda _event: submit())
        dialog.columnconfigure(1, weight=1)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()
        (entries.get("old") or entries.get("current")).focus_set()

    def erste_router_einrichtung(self):
        """Guide a new user through the router address and polling defaults."""
        global BASE_URL
        global POLL_INTERVAL

        dialog = tk.Toplevel(self.root)
        dialog.title("Cudy Auto Refill – Ersteinrichtung")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Mit dem Cudy-Router verbinden",
            font=("Segoe UI", 14, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Verbinde diesen PC per WLAN oder LAN mit deinem Cudy-Router. "
                "Die App verwendet standardmäßig http://192.168.10.1. "
                "Im nächsten Schritt wird dein Admin-Passwort geprüft oder geändert."
            ),
            wraplength=520,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 16))

        ttk.Label(frame, text="Router-Adresse:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        router_url_var = tk.StringVar(value=BASE_URL)
        router_entry = ttk.Entry(frame, textvariable=router_url_var, width=42)
        router_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(frame, text="SMS-Prüfintervall:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        poll_seconds = (5, 10, 15, 30, 60, 120, 300)
        poll_values = [
            intervall_als_text(value, "sekunden")
            for value in poll_seconds
        ]
        initial_poll_interval = (
            POLL_INTERVAL if POLL_INTERVAL in poll_seconds else 10
        )
        poll_var = tk.StringVar(
            value=intervall_als_text(initial_poll_interval, "sekunden")
        )
        poll_combo = ttk.Combobox(
            frame,
            textvariable=poll_var,
            values=poll_values,
            state="readonly",
            width=18,
        )
        poll_combo.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(
            frame,
            text="10 Sekunden empfohlen",
        ).grid(row=3, column=2, sticky="w", padx=(8, 0), pady=4)

        status_var = tk.StringVar(value="")
        ttk.Label(
            frame,
            textvariable=status_var,
            wraplength=520,
            justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        test_button = ttk.Button(buttons, text="Router-Verbindung testen")
        test_button.pack(side="left", padx=(0, 8))
        continue_button = ttk.Button(buttons, text="Speichern und weiter")
        continue_button.pack(side="left")

        busy = False

        def router_verbindung_testen():
            nonlocal busy
            if busy:
                return
            try:
                router_url = validiere_router_basis_url(router_url_var.get())
            except ValueError as exc:
                status_var.set(str(exc))
                return

            if FIRST_START_TEST_MODE:
                status_var.set(
                    "Erststart-Test: Verbindungstest simuliert; kein Router wurde kontaktiert."
                )
                return

            busy = True
            test_button.config(state="disabled")
            continue_button.config(state="disabled")
            status_var.set("Prüfe die Verbindung zum Router …")

            def worker():
                try:
                    response = requests.get(
                        f"{router_url}/cgi-bin/luci/",
                        timeout=ROUTER_AUTH_TIMEOUT,
                    )
                    result = (
                        True,
                        f"Der Router antwortet (HTTP {response.status_code}). "
                        "Die Netzwerkverbindung steht; die Anmeldung wird "
                        "im nächsten Schritt geprüft.",
                    )
                except Exception:
                    result = (
                        False,
                        "Router nicht erreichbar. Prüfe die WLAN-/LAN-Verbindung "
                        "und die Router-Adresse; du kannst sie unten anpassen.",
                    )

                def finish():
                    nonlocal busy
                    busy = False
                    if not dialog.winfo_exists():
                        return
                    status_var.set(result[1])
                    test_button.config(state="normal")
                    continue_button.config(state="normal")

                try:
                    self.root.after(0, finish)
                except tk.TclError:
                    pass

            starte_hintergrund_thread(worker, name="cudy-first-run-connection-check")

        test_button.config(command=router_verbindung_testen)

        def speichern_und_weiter():
            global BASE_URL
            global POLL_INTERVAL

            try:
                werte = validiere_einstellungen_werte({
                    "router_url": router_url_var.get(),
                    "poll_interval": sekunden_aus_text(poll_var.get()),
                    "session_refresh_interval": SESSION_REFRESH_INTERVAL,
                    "watchdog_timeout": WATCHDOG_TIMEOUT,
                })
            except ValueError as exc:
                messagebox.showerror(
                    "Einstellungen ungültig",
                    str(exc),
                    parent=dialog,
                )
                return

            neue_einstellungen = {
                **APP_SETTINGS,
                **werte,
                "router_connection_setup_done": True,
            }
            if not speichere_app_einstellungen(neue_einstellungen):
                messagebox.showerror(
                    "Einrichtung nicht gespeichert",
                    "Die Grundeinstellungen konnten nicht im Windows-Benutzerprofil "
                    "gespeichert werden. Prüfe die Berechtigungen und versuche es erneut.",
                    parent=dialog,
                )
                return

            BASE_URL = werte["router_url"]
            POLL_INTERVAL = werte["poll_interval"]
            self.router_entry.delete(0, "end")
            self.router_entry.insert(0, BASE_URL)
            self.poll_interval_var.set(
                intervall_als_text(POLL_INTERVAL, "sekunden")
            )
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.erste_app_einrichtung()

        continue_button.config(command=speichern_und_weiter)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.columnconfigure(1, weight=1)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()
        router_entry.focus_set()

    def erste_app_einrichtung(self):
        global PASSWORT

        if APP_SETTINGS.get("router_password_security_prompt_done"):
            self.erster_start_speedtest()
            return

        if not APP_SETTINGS.get("router_connection_setup_done"):
            self.erste_router_einrichtung()
            return

        if not PASSWORT:
            PASSWORT = lade_router_passwort()

        bereits_geaendert = messagebox.askyesnocancel(
            "Router-Passwort absichern",
            "Hast du das werkseitige Admin-Passwort deines Cudy-Routers "
            "bereits geändert?\n\n"
            "Ein eigenes Passwort wird für mehr Sicherheit empfohlen.",
            parent=self.root,
        )

        def setup_fertigstellen():
            if not speichere_app_einstellungen({
                **APP_SETTINGS,
                "router_password_security_prompt_done": True,
            }):
                messagebox.showwarning(
                    "Hinweis konnte nicht gespeichert werden",
                    "Die Einrichtung ist abgeschlossen, aber die App konnte "
                    "die Auswahl nicht für den nächsten Start speichern.",
                    parent=self.root,
                )

        if bereits_geaendert is None:
            self.erster_start_speedtest()
            return

        if bereits_geaendert:
            self.router_passwort_dialog(
                "update",
                on_success=setup_fertigstellen,
                on_close=self.erster_start_speedtest,
            )
            return

        jetzt_aendern = messagebox.askyesno(
            "Router-Passwort ändern",
            "Möchtest du das werkseitige Router-Passwort jetzt ändern?",
            parent=self.root,
        )
        if jetzt_aendern:
            self.router_passwort_dialog(
                "change",
                on_success=setup_fertigstellen,
                on_close=self.erster_start_speedtest,
            )
            return

        setup_fertigstellen()
        self.erster_start_speedtest()

    # ========================================================
    # STOP
    # ========================================================

    def stoppen(self):

        global programm_laeuft

        if not programm_laeuft:
            return

        self.download_modus_beenden(grund="Überwachung gestoppt")

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
        self.start_button.config(
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

        starte_hintergrund_thread(worker, name="cudy-sms-refresh")

    # ========================================================
    # SIGNAL AKTUALISIEREN
    # ========================================================

    def erster_start_speedtest(self):
        if not getattr(sys, "frozen", False) and not FIRST_START_TEST_MODE:
            return
        profile = lade_lte_profil()
        if profile.get("erststart_speedtest_gezeigt"):
            return
        profile["erststart_speedtest_gezeigt"] = True
        speichere_lte_profil(profile)
        speedtest_hinweis = (
            "Möchtest du einen LTE-Bandvergleich simulieren?\n\n"
            "Im Erststart-Test wird kein LTE-Band geändert und kein Datenvolumen geladen."
            if FIRST_START_TEST_MODE
            else (
                "Möchtest du jetzt alle verfügbaren LTE-Bänder vergleichen?\n\n"
                "Der Router verbindet sich für jedes Band neu. Bei zehn Bändern "
                "werden bis zu etwa 80 MB Mobilfunkdaten geladen.\n\n"
                "Du kannst den Test später im Reiter „Signal“ erneut starten."
            )
        )
        if messagebox.askyesno(
            "LTE-Einrichtung",
            speedtest_hinweis,
            parent=self.root
        ):
            self.notebook.select(self.signal_tab)
            self.root.update_idletasks()
            self.speedtest_alle_baender(bestaetigung=False)

    def speedtest_abbrechen(self):
        if self._band_test_cancel is not None:
            self._band_test_cancel.set()
            self.band_speedtest_status.set(
                "Abbruch angefordert; die vorherige Bandwahl wird wiederhergestellt …"
            )

    def speedtest_alle_baender(self, bestaetigung=True):
        if FIRST_START_TEST_MODE:
            self.band_speedtest_results.config(state="normal")
            self.band_speedtest_results.delete("1.0", "end")
            self.band_speedtest_results.insert(
                "1.0",
                "SIMULIERTE ERGEBNISSE\n"
                "Download: B28 (42,0 Mbit/s)\n"
                "Gaming/Streaming: B3 (Ping 31 ms, Jitter 4 ms)\n\n"
                "Beispielwerte – es wurde kein Router angesprochen und kein Testdatenvolumen geladen.",
            )
            self.band_speedtest_results.config(state="disabled")
            self.band_speedtest_status.set("LTE-Bandvergleich simuliert; keine Routeränderung.")
            print("Erststart-Test: LTE-Bandvergleich simuliert; kein Routerzugriff.")
            return

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

        starte_hintergrund_thread(worker, name="cudy-band-speedtest")

    # ========================================================
    # SIGNAL AKTUALISIEREN
    # ========================================================

    def signal_aktualisieren(self):
        if FIRST_START_TEST_MODE:
            self.signal_value.config(text="Testmodus: Automatische Bandwahl (simuliert)")
            return

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

        starte_hintergrund_thread(worker, name="cudy-band-refresh")

    # ========================================================
    # TEST REFILL
    # ========================================================

    def test_refill(self):
        if FIRST_START_TEST_MODE:
            aktions_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"
            log_refill_aktion(
                aktions_id,
                "SIMULATION",
                "Manueller Refill simuliert; es wurde keine SMS gesendet.",
            )
            gui_status("letzter_refill", "Simuliert")
            print("Erststart-Test: Manueller Refill simuliert; keine SMS versendet.")
            return

        if not messagebox.askyesno(
            "Notfall-Refill senden?",
            f"Jetzt '{REFILL_TEXT}' per SMS an {LIDL_ABSENDER} senden?\n\n"
            "Dadurch kann beim Mobilfunkanbieter ein Refill ausgelöst werden.",
            parent=self.root
        ):
            return

        self.test_refill_button.config(state="disabled")

        def worker():
            aktions_id = f"MAN-{uuid.uuid4().hex[:8].upper()}"
            log_refill_aktion(
                aktions_id,
                "ERKANNT",
                "Manueller Refill wurde im GUI ausgelöst.",
            )
            log_refill_aktion(
                aktions_id,
                "ENTSCHEIDUNG",
                "Versand nach Bestätigung im Sicherheitsdialog freigegeben.",
            )
            print("Manueller Refill gestartet: sende 'Refill' an 80808.")
            versand_gestartet = False
            outbox_vorher = None
            lock_acquired = False
            try:
                if not REFILL_SEND_LOCK.acquire(blocking=False):
                    log_refill_aktion(
                        aktions_id,
                        "VERSAND ABGEBROCHEN",
                        "Eine andere Refill-Aktion läuft bereits.",
                    )
                    print(
                        "Manueller Refill abgebrochen: eine andere Refill-Aktion läuft bereits."
                    )
                    return
                else:
                    lock_acquired = True
                    notiere_download_refill_versuch(aktions_id)
                    if not router_session_sicherstellen():
                        log_refill_aktion(
                            aktions_id,
                            "VERSAND ABGEBROCHEN",
                            "Die Router-Sitzung konnte nicht hergestellt werden.",
                        )
                        print("Manueller Refill fehlgeschlagen: keine gültige Router-Session.")
                        return

                    try:
                        log_refill_aktion(
                            aktions_id,
                            "VERSAND VORBEREITEN",
                            "Lese den Outbox-Stand vor dem manuellen Refill.",
                        )
                        vorher = outbox_snapshot()
                        outbox_vorher = vorher
                    except Exception:
                        log_refill_aktion(
                            aktions_id,
                            "VERSAND ABGEBROCHEN",
                            "Der Outbox-Ausgangsstand konnte nicht gelesen werden.",
                        )
                        melde_fehler(
                            "outbox",
                            "Die Outbox konnte vor dem manuellen Refill nicht gelesen werden; der Versand wurde abgebrochen.",
                            art="read",
                        )
                        print("Manueller Refill fehlgeschlagen: Outbox konnte nicht gelesen werden.")
                        return
                    with OUTBOX_PENDING_LOCK:
                        bestaetigung_offen = OUTBOX_PENDING_BASELINE is not None
                    if bestaetigung_offen:
                        log_refill_aktion(
                            aktions_id,
                            "VERSAND ABGEBROCHEN",
                            "Eine vorherige Refill-SMS wartet noch auf Outbox-Bestätigung.",
                        )
                        melde_fehler(
                            "outbox",
                            "Der manuelle Refill wurde nicht gesendet, weil eine vorherige Refill-SMS noch unbestätigt ist.",
                            art="confirmation",
                        )
                        print(
                            "Manueller Refill abgebrochen: vorherige Refill-Bestätigung steht noch aus."
                        )
                        return
                    bereinige_fehler("outbox", nur_art="read")

                    versand_gestartet = True
                    log_refill_aktion(
                        aktions_id,
                        "VERSAND",
                        "Sende die manuelle Refill-SMS über den Router.",
                    )
                    if not sende_neue_sms(LIDL_ABSENDER, REFILL_TEXT):
                        log_refill_aktion(
                            aktions_id,
                            "VERSAND FEHLGESCHLAGEN",
                            "Der Router hat die SMS nicht als angenommen bestätigt.",
                        )
                        melde_fehler(
                            "outbox",
                            "Der manuelle Refill wurde vom Router nicht als erfolgreich angenommen.",
                            art="confirmation",
                        )
                        print("Manueller Refill fehlgeschlagen: SMS wurde nicht an den Router übermittelt.")
                        return

                    log_refill_aktion(
                        aktions_id,
                        "VERSAND ANGENOMMEN",
                        "Der Router hat die SMS-Anfrage angenommen.",
                    )
                    if not warte_auf_outbox_bestaetigung(vorher, aktions_id):
                        print("Manueller Refill noch nicht bestätigt; die Outbox wird weiter geprüft.")
                        return

                print("Manueller Refill erfolgreich: neue Refill-SMS in der Outbox bestätigt.")
            except Exception as exc:
                log_refill_aktion(
                    aktions_id,
                    "ERGEBNIS",
                    "Manueller Versand oder Bestätigung ist fehlgeschlagen.",
                )
                print("Manueller Refill fehlgeschlagen:", exc)
                if versand_gestartet:
                    if outbox_vorher is not None:
                        merke_ausstehende_outbox_bestaetigung(outbox_vorher, aktions_id)
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
                if lock_acquired:
                    REFILL_SEND_LOCK.release()
                try:
                    self.root.after(0, lambda: self.test_refill_button.config(state="normal"))
                except tk.TclError:
                    pass

        starte_hintergrund_thread(worker, name="cudy-manual-refill")

    def download_modus_umschalten(self):
        global DOWNLOAD_MODE_ACTIVE
        global DOWNLOAD_MODE_STARTED_AT
        global DOWNLOAD_PROACTIVE_ENABLED
        global DOWNLOAD_PROACTIVE_STARTED_AT
        global DOWNLOAD_PROACTIVE_NEXT_AT
        global DOWNLOAD_PROACTIVE_ATTEMPTS
        global DOWNLOAD_PROACTIVE_BLOCKED
        global DOWNLOAD_PROACTIVE_LAST_RESULT

        with DOWNLOAD_MODE_LOCK:
            aktuell_aktiv = DOWNLOAD_MODE_ACTIVE
            laeuft_refill = DOWNLOAD_PROACTIVE_IN_PROGRESS

        if aktuell_aktiv:
            with DOWNLOAD_MODE_LOCK:
                DOWNLOAD_MODE_ACTIVE = False
                DOWNLOAD_PROACTIVE_ENABLED = False
                DOWNLOAD_PROACTIVE_STARTED_AT = None
                DOWNLOAD_PROACTIVE_NEXT_AT = None
                DOWNLOAD_PROACTIVE_ATTEMPTS = 0
                DOWNLOAD_PROACTIVE_BLOCKED = False
                DOWNLOAD_MODE_STARTED_AT = None
                DOWNLOAD_PROACTIVE_LAST_RESULT = (
                    "Download-Modus ausgeschaltet; normale Laufzeitwerte gelten wieder."
                    if not laeuft_refill
                    else "Download-Modus ausgeschaltet; der laufende Versand wird noch abgeschlossen."
                )
            self.download_proactive_var.set(False)
            print("Download-Modus ausgeschaltet; normale Laufzeitwerte gelten wieder.")
            return

        if laeuft_refill:
            messagebox.showinfo(
                "Refill wird noch geprüft",
                "Ein Refill-Zyklus wird noch auf die Outbox-Bestätigung geprüft. "
                "Schalte den Download-Modus wieder ein, sobald dieser Vorgang beendet ist.",
                parent=self.root,
            )
            return

        with DOWNLOAD_MODE_LOCK:
            DOWNLOAD_MODE_ACTIVE = True
            DOWNLOAD_MODE_STARTED_AT = time.monotonic()
            DOWNLOAD_PROACTIVE_ENABLED = False
            DOWNLOAD_PROACTIVE_STARTED_AT = None
            DOWNLOAD_PROACTIVE_NEXT_AT = None
            DOWNLOAD_PROACTIVE_ATTEMPTS = 0
            DOWNLOAD_PROACTIVE_BLOCKED = False
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Proaktive Refill-SMS sind aus. Inbox-Trigger bleiben aktiv."
            )
        self.download_proactive_var.set(False)
        print(
            "Download-Modus eingeschaltet: Poll 5s, Session-Refresh 60s, "
            "Watchdog 30s; proaktive SMS bleiben ausgeschaltet."
        )

    def download_modus_beenden(self, grund="Download-Modus ausgeschaltet"):
        global DOWNLOAD_MODE_ACTIVE
        global DOWNLOAD_MODE_STARTED_AT
        global DOWNLOAD_PROACTIVE_ENABLED
        global DOWNLOAD_PROACTIVE_STARTED_AT
        global DOWNLOAD_PROACTIVE_NEXT_AT
        global DOWNLOAD_PROACTIVE_ATTEMPTS
        global DOWNLOAD_PROACTIVE_BLOCKED
        global DOWNLOAD_PROACTIVE_LAST_RESULT

        with DOWNLOAD_MODE_LOCK:
            laeuft_refill = DOWNLOAD_PROACTIVE_IN_PROGRESS
            DOWNLOAD_MODE_ACTIVE = False
            DOWNLOAD_MODE_STARTED_AT = None
            DOWNLOAD_PROACTIVE_ENABLED = False
            DOWNLOAD_PROACTIVE_STARTED_AT = None
            DOWNLOAD_PROACTIVE_NEXT_AT = None
            DOWNLOAD_PROACTIVE_ATTEMPTS = 0
            DOWNLOAD_PROACTIVE_BLOCKED = False
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                f"{grund}; der laufende SMS-Versand wird noch abgeschlossen."
                if laeuft_refill
                else f"{grund}; normale Laufzeitwerte gelten wieder."
            )
        self.download_proactive_var.set(False)
        print("Download-Modus beendet:", grund)

    def download_einstellungen_aktualisieren(self, _event=None):
        global DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
        global DOWNLOAD_PROACTIVE_MAX_RUNTIME_SECONDS
        global DOWNLOAD_PROACTIVE_MAX_REFILLS
        global DOWNLOAD_PROACTIVE_NEXT_AT

        intervalle = {
            "60 Sekunden": 60,
            "90 Sekunden": 90,
            "2 Minuten": 120,
            "5 Minuten": 300,
            "10 Minuten": 600,
        }
        laufzeiten = {
            "1 Stunde": 1,
            "2 Stunden": 2,
            "4 Stunden": 4,
            "8 Stunden": 8,
            "12 Stunden": 12,
        }

        with DOWNLOAD_MODE_LOCK:
            DOWNLOAD_PROACTIVE_INTERVAL_SECONDS = intervalle.get(
                self.download_interval_var.get(), 60
            )
            DOWNLOAD_PROACTIVE_MAX_RUNTIME_SECONDS = (
                laufzeiten.get(self.download_duration_var.get(), 12) * 60 * 60
            )
            DOWNLOAD_PROACTIVE_MAX_REFILLS = max(
                1,
                min(40, int(self.download_max_refills_var.get() or "20")),
            )
            if (
                DOWNLOAD_MODE_ACTIVE
                and DOWNLOAD_PROACTIVE_ENABLED
                and not DOWNLOAD_PROACTIVE_IN_PROGRESS
                and not DOWNLOAD_PROACTIVE_BLOCKED
            ):
                DOWNLOAD_PROACTIVE_NEXT_AT = (
                    time.monotonic() + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
                )

        aktualisiere_download_laufzeitlimit()

    def download_proaktiv_umschalten(self):
        global DOWNLOAD_PROACTIVE_ENABLED
        global DOWNLOAD_PROACTIVE_STARTED_AT
        global DOWNLOAD_PROACTIVE_NEXT_AT
        global DOWNLOAD_PROACTIVE_BLOCKED
        global DOWNLOAD_PROACTIVE_LAST_RESULT

        aktivieren = self.download_proactive_var.get()
        if not aktivieren:
            with DOWNLOAD_MODE_LOCK:
                DOWNLOAD_PROACTIVE_ENABLED = False
                DOWNLOAD_PROACTIVE_NEXT_AT = None
                DOWNLOAD_PROACTIVE_LAST_RESULT = (
                    "Proaktive Refill-Zyklen manuell ausgeschaltet."
                    if not DOWNLOAD_PROACTIVE_IN_PROGRESS
                    else "Keine weiteren Zyklen; laufender Versand wird noch bestätigt."
                )
            print("Download-Modus: proaktive Refill-Zyklen ausgeschaltet.")
            return

        with DOWNLOAD_MODE_LOCK:
            modus_aktiv = DOWNLOAD_MODE_ACTIVE
            blockiert = DOWNLOAD_PROACTIVE_BLOCKED
            anzahl = DOWNLOAD_PROACTIVE_ATTEMPTS
            maximum = DOWNLOAD_PROACTIVE_MAX_REFILLS

        if not modus_aktiv:
            messagebox.showinfo(
                "Download-Modus zuerst einschalten",
                "Schalte zuerst den Download-Modus ein. Proaktive Refill-SMS bleiben sonst deaktiviert.",
                parent=self.root,
            )
            self.download_proactive_var.set(False)
            return

        if not programm_laeuft:
            messagebox.showinfo(
                "Überwachung zuerst starten",
                "Starte zuerst die SMS-Überwachung. Proaktive Refill-SMS werden nur "
                "während einer aktiven Überwachung geplant.",
                parent=self.root,
            )
            self.download_proactive_var.set(False)
            return

        self.download_einstellungen_aktualisieren()
        intervalle = {
            "60 Sekunden": 60,
            "90 Sekunden": 90,
            "2 Minuten": 120,
            "5 Minuten": 300,
            "10 Minuten": 600,
        }
        interval = intervalle.get(self.download_interval_var.get(), 60)
        laufzeit = self.download_duration_var.get()
        maximum = int(self.download_max_refills_var.get() or "20")
        hinweis = (
            f"Die App darf ohne passenden Inbox-Trigger 'Refill' an 80808 senden.\n\n"
            f"Mindestabstand: {self.download_interval_var.get()}\n"
            f"Maximale Laufzeit: {laufzeit}\n"
            f"Maximale Anzahl: {maximum} SMS\n"
            f"Die erste SMS wird frühestens nach {interval} Sekunden gesendet.\n\n"
            "Die App prüft nach jedem Versand die Router-Outbox. Bei fehlender "
            "Bestätigung pausiert sie weitere proaktive SMS. Möchtest du das ausdrücklich aktivieren?"
        )
        if blockiert:
            hinweis = (
                "Der vorherige proaktive Refill wurde nicht bestätigt. Prüfe zuerst "
                "die Router-Outbox; nur wenn du dort keinen ausstehenden Refill siehst, "
                "solltest du fortfahren.\n\n" + hinweis
            )

        if not messagebox.askyesno(
            "Proaktive Refill-SMS aktivieren?",
            hinweis,
            parent=self.root,
        ):
            self.download_proactive_var.set(False)
            return

        with DOWNLOAD_MODE_LOCK:
            if anzahl >= DOWNLOAD_PROACTIVE_MAX_REFILLS:
                DOWNLOAD_PROACTIVE_ENABLED = False
                DOWNLOAD_PROACTIVE_NEXT_AT = None
                DOWNLOAD_PROACTIVE_LAST_RESULT = (
                    "Maximale Zahl proaktiver Refills erreicht. Schalte den Download-Modus "
                    "aus und wieder ein, um eine neue begrenzte Sitzung zu starten."
                )
                self.download_proactive_var.set(False)
                return

            if DOWNLOAD_PROACTIVE_STARTED_AT is None:
                DOWNLOAD_PROACTIVE_STARTED_AT = time.monotonic()
            DOWNLOAD_PROACTIVE_ENABLED = True
            DOWNLOAD_PROACTIVE_BLOCKED = False
            with OUTBOX_PENDING_LOCK:
                outbox_offen = OUTBOX_PENDING_BASELINE is not None
            DOWNLOAD_PROACTIVE_NEXT_AT = (
                None
                if outbox_offen
                else time.monotonic() + DOWNLOAD_PROACTIVE_INTERVAL_SECONDS
            )
            DOWNLOAD_PROACTIVE_LAST_RESULT = (
                "Warte auf die Bestätigung einer vorherigen Refill-Aktion."
                if outbox_offen
                else "Proaktive Refill-SMS aktiviert; erster Zyklus wird vorbereitet."
            )

        print(
            "Download-Modus: proaktive Refill-SMS ausdrücklich aktiviert; "
            f"Intervall {self.download_interval_var.get()}, Laufzeit {laufzeit}, "
            f"Limit {maximum}."
        )

    def aktualisiere_download_anzeige(self):
        aktualisiere_download_laufzeitlimit()
        with DOWNLOAD_MODE_LOCK:
            aktiv = DOWNLOAD_MODE_ACTIVE
            proaktiv = DOWNLOAD_PROACTIVE_ENABLED
            in_progress = DOWNLOAD_PROACTIVE_IN_PROGRESS
            blocked = DOWNLOAD_PROACTIVE_BLOCKED
            naechster = DOWNLOAD_PROACTIVE_NEXT_AT
            gestartet = DOWNLOAD_PROACTIVE_STARTED_AT
            versuche = DOWNLOAD_PROACTIVE_ATTEMPTS
            maximum = DOWNLOAD_PROACTIVE_MAX_REFILLS
            max_laufzeit = DOWNLOAD_PROACTIVE_MAX_RUNTIME_SECONDS
            ergebnis = DOWNLOAD_PROACTIVE_LAST_RESULT

        self.download_mode_button.config(
            text=(
                "Download-Modus ausschalten"
                if aktiv
                else "Download-Modus einschalten"
            )
        )
        if self.download_proactive_var.get() != proaktiv:
            self.download_proactive_var.set(proaktiv)
        self.download_proactive_check.config(
            state=(
                "normal"
                if aktiv and programm_laeuft
                else "disabled"
            )
        )

        if aktiv:
            self.download_mode_status_var.set(
                "Aktiv: Poll 5 s, Session-Refresh 60 s, Watchdog 30 s. "
                "Deaktivieren stellt deine normalen Werte wieder her."
            )
        else:
            self.download_mode_status_var.set(
                "Aus: deine normalen Polling-, Refresh- und Watchdog-Werte gelten."
            )

        if not proaktiv:
            self.download_next_cycle_var.set("Proaktive Refill-Zyklen sind ausgeschaltet.")
        elif in_progress:
            self.download_next_cycle_var.set(
                f"Refill-Zyklus wird bestätigt ({versuche}/{maximum})."
            )
        elif blocked:
            self.download_next_cycle_var.set(
                "Automatisch pausiert, bis die vorherige Refill-Aktion geprüft wurde."
            )
        elif naechster is None:
            self.download_next_cycle_var.set(
                f"Proaktive Zyklen aktiv ({versuche}/{maximum}); warte auf Outbox-Prüfung."
            )
        else:
            rest = max(0, int(naechster - time.monotonic() + 0.999))
            minuten, sekunden = divmod(rest, 60)
            laufzeit_rest = max(
                0,
                int(gestartet + max_laufzeit - time.monotonic()),
            ) if gestartet is not None else max_laufzeit
            rest_stunden, rest_sekunden = divmod(laufzeit_rest, 3600)
            rest_minuten = rest_sekunden // 60
            self.download_next_cycle_var.set(
                f"Nächster Refill-Zyklus in {minuten:02d}:{sekunden:02d} "
                f"({versuche}/{maximum}); Laufzeit übrig: "
                f"{rest_stunden:02d}:{rest_minuten:02d}."
            )
        self.download_last_result_var.set(ergebnis)

    # ========================================================
    # BÄNDER
    # ========================================================

    def baender_verwalten(self):
        if FIRST_START_TEST_MODE:
            messagebox.showinfo(
                "LTE-Bänder",
                "Die Bandverwaltung ist im Erststart-Testmodus deaktiviert. "
                "Der Router wird nicht angesprochen.",
                parent=self.root,
            )
            return

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

            starte_hintergrund_thread(apply_worker, name="cudy-band-apply")

        apply_button.config(command=apply_selection)
        starte_hintergrund_thread(load_worker, name="cudy-band-load")

    # ========================================================
    # EINSTELLUNGEN SPEICHERN
    # ========================================================

    def einstellungen_exportieren(self):
        try:
            einstellungen = validiere_einstellungen_werte({
                "router_url": self.router_entry.get().strip() or BASE_URL,
                "poll_interval": sekunden_aus_text(self.poll_interval_var.get()),
                "session_refresh_interval": sekunden_aus_text(
                    self.session_refresh_var.get()
                ),
                "watchdog_timeout": sekunden_aus_text(self.watchdog_var.get()),
            })
        except ValueError as exc:
            messagebox.showerror(
                "Einstellungen können nicht exportiert werden",
                str(exc),
                parent=self.root,
            )
            return

        dateipfad = filedialog.asksaveasfilename(
            parent=self.root,
            title="Einstellungen exportieren",
            defaultextension=".json",
            initialfile="cudy-auto-refill-settings.json",
            filetypes=[("Cudy-Einstellungen", "*.json"), ("Alle Dateien", "*.*")],
        )
        if not dateipfad:
            return

        geschuetzte_pfade = {
            os.path.normcase(os.path.abspath(pfad))
            for pfad in (
                SETTINGS_FILE,
                CREDENTIALS_FILE,
                SMS_STATE_FILE,
                LTE_PROFILE_FILE,
                APP_LOG_FILE,
                os.path.join(NORMAL_APP_DATA_DIR, "settings.json"),
                os.path.join(NORMAL_APP_DATA_DIR, "router_password.dpapi"),
                os.path.join(NORMAL_APP_DATA_DIR, "sms_state.json"),
                os.path.join(NORMAL_APP_DATA_DIR, "lte_profile.json"),
                os.path.join(NORMAL_APP_DATA_DIR, "cudy_auto_refill.log"),
                os.path.join(APP_HOME_DIR, "cudy_credentials.json"),
                os.path.join(APP_HOME_DIR, "sms_state.json"),
                LEGACY_SMS_STATE_FILE,
                *LEGACY_CREDENTIAL_FILES,
            )
            if pfad
        }
        if os.path.normcase(os.path.abspath(dateipfad)) in geschuetzte_pfade:
            messagebox.showerror(
                "Export fehlgeschlagen",
                "Diese Datei wird von der App verwendet und darf nicht überschrieben werden. "
                "Bitte wähle einen anderen Speicherort.",
                parent=self.root,
            )
            return

        export = {
            "format": SETTINGS_EXPORT_FORMAT,
            "version": SETTINGS_EXPORT_VERSION,
            "settings": einstellungen,
        }
        try:
            with open(dateipfad, "w", encoding="utf-8") as datei:
                json.dump(export, datei, ensure_ascii=False, indent=2)
                datei.write("\n")
        except OSError:
            messagebox.showerror(
                "Export fehlgeschlagen",
                "Die Einstellungsdatei konnte nicht gespeichert werden.",
                parent=self.root,
            )
            return

        messagebox.showinfo(
            "Export abgeschlossen",
            "Die Einstellungen wurden exportiert. Passwörter, Tokens und SMS-Daten "
            "sind nicht enthalten.",
            parent=self.root,
        )

    def einstellungen_importieren(self):
        if programm_laeuft or self._band_test_running:
            messagebox.showinfo(
                "Import gerade nicht möglich",
                "Bitte stoppe zuerst die Überwachung und laufende LTE-Tests.",
                parent=self.root,
            )
            return

        dateipfad = filedialog.askopenfilename(
            parent=self.root,
            title="Einstellungen importieren",
            filetypes=[("Cudy-Einstellungen", "*.json"), ("Alle Dateien", "*.*")],
        )
        if not dateipfad:
            return

        try:
            if os.path.getsize(dateipfad) > 64 * 1024:
                raise ValueError("Die Einstellungsdatei ist zu groß.")
            with open(dateipfad, "r", encoding="utf-8") as datei:
                importierte_einstellungen = validiere_einstellungen_export(
                    json.load(datei)
                )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            messagebox.showerror(
                "Import fehlgeschlagen",
                f"Die Datei wurde nicht angewendet. {exc}",
                parent=self.root,
            )
            return

        zusammenfassung = (
            f"Router-Adresse: {importierte_einstellungen['router_url']}\n"
            f"Poll-Intervall: {intervall_als_text(importierte_einstellungen['poll_interval'], 'sekunden')}\n"
            f"Session-Refresh: {intervall_als_text(importierte_einstellungen['session_refresh_interval'], 'minuten')}\n"
            f"Watchdog: {intervall_als_text(importierte_einstellungen['watchdog_timeout'], 'sekunden')}\n\n"
            "Nur diese vier Werte werden übernommen. Zugangsdaten, Tokens und SMS-Daten "
            "bleiben auf diesem Gerät unverändert."
        )
        if not messagebox.askyesno(
            "Einstellungen übernehmen?",
            zusammenfassung,
            parent=self.root,
        ):
            return

        neue_einstellungen = dict(APP_SETTINGS)
        neue_einstellungen.update(importierte_einstellungen)
        if not speichere_app_einstellungen(neue_einstellungen):
            messagebox.showerror(
                "Import fehlgeschlagen",
                "Die Einstellungen konnten nicht gespeichert werden. "
                "Die bisherige Konfiguration bleibt aktiv.",
                parent=self.root,
            )
            return

        global BASE_URL
        global POLL_INTERVAL
        global SESSION_REFRESH_INTERVAL
        global WATCHDOG_TIMEOUT
        global letzter_session_refresh
        global letzter_erfolgreicher_poll

        BASE_URL = importierte_einstellungen["router_url"]
        POLL_INTERVAL = importierte_einstellungen["poll_interval"]
        SESSION_REFRESH_INTERVAL = importierte_einstellungen[
            "session_refresh_interval"
        ]
        WATCHDOG_TIMEOUT = importierte_einstellungen["watchdog_timeout"]

        self.router_entry.delete(0, tk.END)
        self.router_entry.insert(0, BASE_URL)
        self.poll_interval_var.set(intervall_als_text(POLL_INTERVAL, "sekunden"))
        self.session_refresh_var.set(
            intervall_als_text(SESSION_REFRESH_INTERVAL, "minuten")
        )
        self.watchdog_var.set(intervall_als_text(WATCHDOG_TIMEOUT, "sekunden"))

        jetzt = time.monotonic()
        letzter_session_refresh = jetzt
        letzter_erfolgreicher_poll = jetzt
        print("Einstellungen aus Datei importiert und gespeichert.")
        gui_status("status", "Einstellungen importiert")

    def einstellungen_speichern(self):

        global BASE_URL
        global POLL_INTERVAL
        global SESSION_REFRESH_INTERVAL
        global WATCHDOG_TIMEOUT
        global letzter_session_refresh
        global letzter_erfolgreicher_poll

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

        neue_einstellungen = dict(APP_SETTINGS)
        neue_einstellungen.update({
            "router_url": BASE_URL,
            "poll_interval": POLL_INTERVAL,
            "session_refresh_interval": SESSION_REFRESH_INTERVAL,
            "watchdog_timeout": WATCHDOG_TIMEOUT,
        })
        if not speichere_app_einstellungen(neue_einstellungen):
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

        aktive_worker = aktive_hintergrund_threads()

        if programm_laeuft:
            self.start_button.config(state="disabled")
            self.stop_button.config(state="normal")

        elif aktive_worker:
            self.start_button.config(state="disabled")
            self.stop_button.config(state="disabled")

        else:

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

        self.aktualisiere_download_anzeige()
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

        if self._closing:
            return

        self._closing = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="disabled")

        self.download_modus_beenden(grund="App geschlossen")

        if programm_laeuft:

            programm_laeuft = False

            print()

            print(
                "Cudy Auto Refill wird beendet..."
            )

        self._warte_auf_worker_vor_schliessen()

    def _warte_auf_worker_vor_schliessen(self):
        if aktive_hintergrund_threads():
            if not self._shutdown_wait_notice_written:
                print(
                    "Warte auf laufende Router-Aktionen, bevor die App geschlossen wird..."
                )
                self._shutdown_wait_notice_written = True
            try:
                self.root.after(100, self._warte_auf_worker_vor_schliessen)
            except tk.TclError:
                pass
            return

        self.root.destroy()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:
        einzelne_instanz = acquire_single_instance_lock()
    except OSError as exc:
        start_root = tk.Tk()
        start_root.withdraw()
        messagebox.showerror(
            "Cudy Auto Refill konnte nicht starten",
            "Die Instanz-Sperre konnte nicht angelegt werden. "
            "Zum Schutz vor doppelten Refill-Aktionen wurde der Start abgebrochen.\n\n"
            f"Details: {exc}",
            parent=start_root,
        )
        start_root.destroy()
        raise SystemExit(1)

    if not einzelne_instanz:
        start_root = tk.Tk()
        start_root.withdraw()
        messagebox.showinfo(
            "Instanz-Sperre verhindert den Start",
            "Eine weitere Instanz läuft bereits oder die Instanz-Sperre ist nicht verfügbar. "
            "Diese Instanz startet deshalb keinen zusätzlichen Polling- oder Refill-Loop.",
            parent=start_root,
        )
        start_root.destroy()
        raise SystemExit(0)

    import sys

    original_stdout = sys.stdout

    output_logger = QueueLogger(original_stdout)
    sys.stdout = output_logger
    if sys.stderr is None:
        sys.stderr = output_logger

    root = tk.Tk()

    app = CudyGUI(
        root
    )

    root.mainloop()
