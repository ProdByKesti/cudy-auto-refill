import statistics
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LTE_BANDS = ("1", "3", "5", "7", "8", "20", "28", "38", "40", "41")
CONFIG_PATH = "/cgi-bin/luci/admin/network/gcom/config/apn?embedded=&iface=4g"
DOWNLOAD_URL = "https://speed.cloudflare.com/__down"
DOWNLOAD_BYTES = 4_000_000
RECONNECT_SECONDS = 45


def _form(session, base_url, ensure_session):
    if not ensure_session():
        raise RuntimeError("Keine gültige Router-Session.")
    url = urljoin(base_url.rstrip("/") + "/", CONFIG_PATH.lstrip("/"))
    response = session.get(url, timeout=15)
    if response.status_code == 403 or BeautifulSoup(response.text, "html.parser").find(
        "input", {"name": "luci_password"}
    ):
        if not ensure_session():
            raise RuntimeError("Router-Login fehlgeschlagen.")
        response = session.get(url, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for form in soup.find_all("form", {"name": "cbi"}):
        names = {
            item.get("name") for item in form.find_all(["input", "select", "textarea"])
            if item.get("name")
        }
        if {"cbid.network.4g.selband", "cbid.network.4g.ltebands"} <= names:
            return response, form
    raise ValueError("LTE-Bandformular wurde nicht gefunden.")


def _fields(form):
    result = []
    for item in form.find_all(["input", "select", "textarea", "button"]):
        name = item.get("name")
        if not name or item.has_attr("disabled"):
            continue
        if item.name == "input":
            kind = (item.get("type") or "text").lower()
            if kind in {"submit", "button", "reset", "image", "file"}:
                continue
            if kind in {"checkbox", "radio"} and not item.has_attr("checked"):
                continue
            result.append((name, item.get("value", "")))
        elif item.name == "select":
            options = item.find_all("option", selected=True)
            if not options and not item.has_attr("multiple"):
                options = [item.find("option")] if item.find("option") else []
            result.extend((name, opt.get("value", opt.get_text(strip=True))) for opt in options)
        elif item.name == "textarea":
            result.append((name, item.get_text()))
        elif name == "cbi.apply":
            result.append((name, item.get("value", "")))
    return result


def read_band_config(session, base_url, ensure_session):
    _, form = _form(session, base_url, ensure_session)
    fields = _fields(form)
    available, selected = [], []
    for item in form.find_all("input", {"name": "cbid.network.4g.ltebands"}):
        band = item.get("value", "")
        if band and band not in available:
            available.append(band)
        if band and item.has_attr("checked") and band not in selected:
            selected.append(band)
    if not available:
        raise ValueError("Der Router meldet keine LTE-Bänder.")
    return {
        "automatic": dict(fields).get("cbid.network.4g.selband", "0") != "1",
        "available": tuple(available),
        "selected": selected,
    }


def apply_band_config(session, base_url, ensure_session, automatic, bands):
    response, form = _form(session, base_url, ensure_session)
    available = {
        item.get("value") for item in form.find_all(
            "input", {"name": "cbid.network.4g.ltebands"}
        ) if item.get("value")
    }
    selected = tuple(dict.fromkeys(str(b) for b in bands))
    if not automatic and not selected:
        raise ValueError("Mindestens ein LTE-Band auswählen.")
    if any(b not in available for b in selected):
        raise ValueError("Die Auswahl enthält ein unbekanntes LTE-Band.")
    fields = [
        (name, value) for name, value in _fields(form)
        if name not in {"cbid.network.4g.selband", "cbid.network.4g.ltebands", "cbi.apply"}
    ]
    fields.append(("cbid.network.4g.selband", "0" if automatic else "1"))
    fields.extend(("cbid.network.4g.ltebands", band) for band in selected)
    fields.append(("cbi.apply", ""))
    if "cbi.submit" not in {name for name, _ in fields}:
        fields.append(("cbi.submit", "1"))
    if "timeclock" in {name for name, _ in fields}:
        fields = [(n, str(int(time.time())) if n == "timeclock" else v) for n, v in fields]
    action = form.get("action")
    if not action:
        raise ValueError("Das Ziel des LTE-Bandformulars fehlt.")
    result = session.post(
        urljoin(response.url, action), data=fields,
        headers={"Referer": response.url}, timeout=30,
    )
    if result.status_code == 403:
        raise PermissionError("Router-Session beim Speichern abgelaufen.")
    result.raise_for_status()
    return True


def _cancelled(event):
    return event is not None and event.is_set()


def _internet_ready(net, event, progress):
    end = time.monotonic() + RECONNECT_SECONDS
    while time.monotonic() < end and not _cancelled(event):
        try:
            response = net.get(
                DOWNLOAD_URL, params={"bytes": 0, "check": time.monotonic_ns()},
                timeout=(5, 6),
            )
            response.raise_for_status()
            response.close()
            return True
        except requests.RequestException:
            if progress:
                progress("Warte auf LTE-Verbindung …")
            time.sleep(2.5)
    return False


def _measure(net, event):
    pings, ping_errors = [], 0
    for i in range(6):
        if _cancelled(event):
            break
        started = time.perf_counter()
        try:
            r = net.get(
                DOWNLOAD_URL, params={"bytes": 0, "check": f"{time.monotonic_ns()}-{i}"},
                timeout=(5, 8),
            )
            r.raise_for_status()
            r.close()
            pings.append((time.perf_counter() - started) * 1000)
        except requests.RequestException:
            ping_errors += 1

    speeds, download_errors = [], 0
    for i in range(2):
        if _cancelled(event):
            break
        started, received = time.perf_counter(), 0
        try:
            r = net.get(
                DOWNLOAD_URL,
                params={"bytes": DOWNLOAD_BYTES, "check": f"{time.monotonic_ns()}-{i}"},
                timeout=(8, 25), stream=True,
            )
            r.raise_for_status()
            for chunk in r.iter_content(64 * 1024):
                if _cancelled(event):
                    break
                received += len(chunk)
            r.close()
            duration = time.perf_counter() - started
            if received and duration and not _cancelled(event):
                speeds.append(received * 8 / duration / 1_000_000)
            else:
                download_errors += 1
        except requests.RequestException:
            download_errors += 1

    jitter = statistics.mean(abs(b - a) for a, b in zip(pings, pings[1:])) if len(pings) > 1 else None
    total = len(pings) + ping_errors
    return {
        "download_mbps": statistics.median(speeds) if speeds else None,
        "ping_ms": statistics.median(pings) if pings else None,
        "jitter_ms": jitter,
        "loss_percent": ping_errors / total * 100 if total else 100.0,
        "status": "ok" if speeds and pings else "unvollständig",
        "failed_downloads": download_errors,
    }


def run_all_band_speedtest(router_session, base_url, ensure_session, progress=None, cancel_event=None):
    original = read_band_config(router_session, base_url, ensure_session)
    bands = [b for b in LTE_BANDS if b in original["available"]] or list(original["available"])
    results, restored, restore_error = [], False, None
    net = requests.Session()
    net.headers["User-Agent"] = "CudyAutoRefill LTE band comparison"
    operation_error = None
    try:
        for index, band in enumerate(bands, 1):
            if _cancelled(cancel_event):
                break
            try:
                if progress:
                    progress(f"Band B{band}: Verbindung wird aufgebaut ({index}/{len(bands)}) …")
                apply_band_config(router_session, base_url, ensure_session, False, [band])
                if not _internet_ready(net, cancel_event, progress):
                    if _cancelled(cancel_event):
                        break
                    results.append({
                        "band": band, "status": "kein Internet", "download_mbps": None,
                        "ping_ms": None, "jitter_ms": None, "loss_percent": 100.0,
                    })
                    continue
                result = _measure(net, cancel_event)
                result["band"] = band
                results.append(result)
                if progress:
                    progress(f"Band B{band}: Messung abgeschlossen ({index}/{len(bands)}).")
                if _cancelled(cancel_event):
                    break
            except Exception as exc:
                operation_error = str(exc)
                break
    finally:
        try:
            apply_band_config(
                router_session, base_url, ensure_session,
                original["automatic"], original["selected"],
            )
            restored = True
        except Exception as exc:
            restore_error = str(exc)
        net.close()

    usable = [r for r in results if r.get("download_mbps") is not None and r.get("ping_ms") is not None]
    download_best = max(usable, key=lambda r: r["download_mbps"], default=None)
    gaming_best = min(
        usable,
        key=lambda r: (
            r.get("loss_percent", 100), r.get("ping_ms", float("inf")),
            r.get("jitter_ms") if r.get("jitter_ms") is not None else float("inf"),
        ),
        default=None,
    )
    return {
        "results": results, "download_best": download_best,
        "gaming_best": gaming_best, "restored": restored,
        "restore_error": restore_error, "cancelled": _cancelled(cancel_event),
        "error": operation_error,
    }
