import statistics
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LTE_BANDS = ("1", "3", "5", "7", "8", "20", "28", "38", "40", "41")
CONFIG_PATH = "/cgi-bin/luci/admin/network/gcom/config/apn?embedded=&iface=4g"
DOWNLOAD_URL = "https://speed.cloudflare.com/__down"
DOWNLOAD_BYTES = 4_000_000
BAND_TEST_MAX_REPEAT_COUNT = 10
BAND_TEST_DATA_BUDGET_BYTES = 900_000_000
BAND_RECOMMENDATION_WINDOW_DAYS = 14
BAND_RECOMMENDATION_MAX_RUNS = 20
RECONNECT_SECONDS = 45
BAND_VERIFY_SECONDS = 30


def estimate_band_test_data_mb(band_count, repeat_count=1):
    """Estimate payload downloaded by band tests, rounded up to decimal MB."""
    band_count = max(0, int(band_count))
    repeat_count = max(0, int(repeat_count))
    payload_bytes = band_count * repeat_count * DOWNLOAD_BYTES * 2
    return (payload_bytes + 999_999) // 1_000_000


def max_safe_band_test_runs(band_count):
    """Limit a test batch to 900 MB of payload and at most ten full runs."""
    band_count = max(0, int(band_count))
    bytes_per_run = band_count * DOWNLOAD_BYTES * 2
    if bytes_per_run <= 0:
        return 0
    by_data_budget = BAND_TEST_DATA_BUDGET_BYTES // bytes_per_run
    return min(BAND_TEST_MAX_REPEAT_COUNT, by_data_budget)


def recent_band_runs_for_recommendation(history, now=None):
    """Return only dated runs from the recent recommendation window."""
    if not isinstance(history, list):
        return []
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    now = now.astimezone()
    cutoff = now - timedelta(days=BAND_RECOMMENDATION_WINDOW_DAYS)
    dated_runs = []
    for run in history:
        if not isinstance(run, dict):
            continue
        raw_time = run.get("measured_at") or run.get("completed_at")
        try:
            measured_at = datetime.fromisoformat(raw_time)
            if measured_at.tzinfo is None:
                measured_at = measured_at.astimezone()
            measured_at = measured_at.astimezone()
        except (TypeError, ValueError):
            continue
        if cutoff <= measured_at <= now + timedelta(hours=12):
            dated_runs.append((measured_at, run))
    dated_runs.sort(key=lambda item: item[0])
    return [run for _, run in dated_runs[-BAND_RECOMMENDATION_MAX_RUNS:]]


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

    # HTTP 200 bestätigt nur die Anfrage. Cudy übernimmt die neue Auswahl
    # separat; deshalb lesen wir die geschützte Konfigurationsseite zurück.
    expected_bands = set(selected)
    deadline = time.monotonic() + BAND_VERIFY_SECONDS
    last_error = None
    while True:
        try:
            current = read_band_config(session, base_url, ensure_session)
            if current["automatic"] == bool(automatic) and (
                automatic or set(current["selected"]) == expected_bands
            ):
                return True
            last_error = "Router zeigt noch die vorherige Bandwahl."
        except Exception as exc:
            last_error = str(exc)

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Speichern wurde gesendet, aber die Bandwahl ließ sich nicht "
                f"bestätigen: {last_error}"
            )
        time.sleep(1.5)


def _cancelled(event):
    return event is not None and event.is_set()


def _internet_ready(net, event, progress, timeout=RECONNECT_SECONDS):
    end = time.monotonic() + timeout
    request_timeout = (
        min(5, max(1, timeout / 3)),
        min(6, max(1, timeout / 3)),
    )
    while time.monotonic() < end and not _cancelled(event):
        try:
            response = net.get(
                DOWNLOAD_URL, params={"bytes": 0, "check": time.monotonic_ns()},
                timeout=request_timeout,
            )
            response.raise_for_status()
            response.close()
            return True
        except requests.RequestException:
            if progress:
                progress("Warte auf LTE-Verbindung …")
            time.sleep(2.5)
    return False


def wait_for_internet(progress=None, timeout=60):
    """Check internet access after a manual LTE change without downloading data."""
    net = requests.Session()
    net.headers["User-Agent"] = "CudyAutoRefill connection check"
    try:
        return _internet_ready(net, None, progress, timeout)
    finally:
        net.close()


def recover_offline_manual_band(
    session, base_url, ensure_session, progress=None, probe_timeout=6
):
    """Fall back to automatic LTE band selection only when a locked band is offline."""
    current = read_band_config(session, base_url, ensure_session)
    if current["automatic"]:
        return {"status": "already_automatic", "previous": current}

    if wait_for_internet(progress=None, timeout=probe_timeout):
        return {"status": "manual_band_online", "previous": current}

    if progress:
        progress(
            "Mit der gespeicherten manuellen Bandwahl ist das Internet nicht erreichbar; "
            "automatische LTE-Bandwahl wird aktiviert …"
        )
    apply_band_config(session, base_url, ensure_session, True, [])
    return {"status": "automatic_fallback", "previous": current}


def apply_band_config_with_fallback(
    session, base_url, ensure_session, automatic, bands, progress=None, timeout=60
):
    """Apply a band choice and restore the previous config if internet stays down."""
    previous = read_band_config(session, base_url, ensure_session)
    apply_error = None
    try:
        apply_band_config(session, base_url, ensure_session, automatic, bands)
        if wait_for_internet(progress=progress, timeout=timeout):
            return {"status": "applied", "previous": previous}
        apply_error = RuntimeError(
            f"Nach {timeout} Sekunden war keine Internetverbindung erreichbar."
        )
    except Exception as exc:
        apply_error = exc

    if progress:
        progress("Internet fehlt; vorherige Bandwahl wird wiederhergestellt …")
    try:
        apply_band_config(
            session, base_url, ensure_session,
            previous["automatic"], previous["selected"],
        )
        if wait_for_internet(progress=progress, timeout=timeout):
            return {
                "status": "restored", "previous": previous,
                "error": str(apply_error),
            }
        restore_error = "Internet nach Wiederherstellung nicht erreichbar."
    except Exception as exc:
        restore_error = str(exc)

    return {
        "status": "restore_failed", "previous": previous,
        "error": str(apply_error), "restore_error": restore_error,
    }


def _measure(net, event):
    pings, ping_errors, ping_attempts = [], 0, 0
    for i in range(6):
        if _cancelled(event):
            break
        ping_attempts += 1
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

    speeds, download_errors, download_attempts = [], 0, 0
    for i in range(2):
        if _cancelled(event):
            break
        download_attempts += 1
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
        "ping_attempts": ping_attempts,
        "ping_failures": ping_errors,
        "download_attempts": download_attempts,
        "download_successes": len(speeds),
        "probe_attempts": ping_attempts + download_attempts,
        "failed_probes": ping_errors + download_errors,
    }


def measure_current_download_speed():
    """Measure the active internet connection without changing router bands."""
    net = requests.Session()
    net.headers["User-Agent"] = "CudyAutoRefill download interval estimate"
    try:
        result = _measure(net, None)
        if result.get("download_mbps") is None:
            raise RuntimeError("Die aktuelle Downloadgeschwindigkeit konnte nicht gemessen werden.")
        return result
    finally:
        net.close()


def run_all_band_speedtest(
    router_session,
    base_url,
    ensure_session,
    progress=None,
    cancel_event=None,
    repeat_count=1,
    run_complete=None,
):
    try:
        repeat_count = int(repeat_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("Die Anzahl der Testläufe muss eine Zahl von 1 bis 10 sein.") from exc
    if not 1 <= repeat_count <= BAND_TEST_MAX_REPEAT_COUNT:
        raise ValueError(
            "Die Anzahl der Testläufe muss zwischen 1 und "
            f"{BAND_TEST_MAX_REPEAT_COUNT} liegen."
        )

    original = read_band_config(router_session, base_url, ensure_session)
    bands = [b for b in LTE_BANDS if b in original["available"]] or list(original["available"])
    safe_run_limit = max_safe_band_test_runs(len(bands))
    if repeat_count > safe_run_limit:
        estimated_mb = estimate_band_test_data_mb(len(bands), repeat_count)
        raise ValueError(
            f"{repeat_count} Messläufe würden geschätzt {estimated_mb} MB laden. "
            f"Bei {len(bands)} verfügbaren Bändern sind höchstens "
            f"{safe_run_limit} Läufe im 900-MB-Sicherheitsbudget erlaubt."
        )
    runs, restored, restore_error = [], False, None
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    net = requests.Session()
    net.headers["User-Agent"] = "CudyAutoRefill LTE band comparison"
    operation_error = None
    try:
        for run_index in range(1, repeat_count + 1):
            run_results = []
            for index, band in enumerate(bands, 1):
                if _cancelled(cancel_event):
                    break
                try:
                    if progress:
                        progress(
                            f"Messlauf {run_index}/{repeat_count} · Band B{band}: "
                            f"Verbindung wird aufgebaut ({index}/{len(bands)}) …"
                        )
                    apply_band_config(router_session, base_url, ensure_session, False, [band])
                    if not _internet_ready(net, cancel_event, progress):
                        if _cancelled(cancel_event):
                            break
                        run_results.append({
                            "band": band, "status": "kein Internet", "download_mbps": None,
                            "ping_ms": None, "jitter_ms": None, "loss_percent": 100.0,
                            "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "probe_attempts": 1, "failed_probes": 1,
                            "ping_attempts": 0, "ping_failures": 0,
                            "download_attempts": 0, "download_successes": 0,
                        })
                        continue
                    result = _measure(net, cancel_event)
                    result["band"] = band
                    result["measured_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    run_results.append(result)
                    if progress:
                        progress(
                            f"Messlauf {run_index}/{repeat_count} · Band B{band}: "
                            f"Messung abgeschlossen ({index}/{len(bands)})."
                        )
                    if _cancelled(cancel_event):
                        break
                except Exception as exc:
                    operation_error = str(exc)
                    break

            if run_results:
                completed_run = {
                    "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "results": run_results,
                }
                runs.append(completed_run)
                if run_complete and len(run_results) == len(bands):
                    run_complete(completed_run, run_index, repeat_count)
            if _cancelled(cancel_event) or operation_error:
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

    results = [item for run in runs for item in run["results"]]
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
        "runs": runs, "results": results, "download_best": download_best,
        "gaming_best": gaming_best, "restored": restored,
        "restore_error": restore_error, "cancelled": _cancelled(cancel_event),
        "error": operation_error, "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requested_runs": repeat_count, "completed_runs": len(runs),
    }


def summarize_band_history(history, minimum_runs=2):
    """Summarize recent band runs while preserving the complete stored history."""
    if not isinstance(history, list):
        history = []

    by_band = {}
    valid_runs = [run for run in history if isinstance(run, dict)]
    recommendation_runs = recent_band_runs_for_recommendation(valid_runs)
    for run in recommendation_runs:
        run_time = run.get("measured_at") or run.get("completed_at")
        for item in run.get("results", []):
            if not isinstance(item, dict) or item.get("band") is None:
                continue
            record = dict(item)
            record.setdefault("measured_at", run_time)
            by_band.setdefault(str(item["band"]), []).append(record)

    def band_sort_key(item):
        try:
            return int(item[0])
        except (TypeError, ValueError):
            return 999

    bands = []
    for band, records in sorted(by_band.items(), key=band_sort_key):
        usable = [
            row for row in records
            if isinstance(row.get("download_mbps"), (int, float))
            and isinstance(row.get("ping_ms"), (int, float))
        ]
        downloads = [float(row["download_mbps"]) for row in usable]
        pings = [float(row["ping_ms"]) for row in usable]
        jitters = [
            float(row["jitter_ms"]) for row in usable
            if isinstance(row.get("jitter_ms"), (int, float))
        ]
        def zaehler(row, key):
            try:
                return max(0, int(row.get(key, 0) or 0))
            except (TypeError, ValueError):
                return 0

        probe_attempts = sum(zaehler(row, "probe_attempts") for row in records)
        failed_probes = sum(zaehler(row, "failed_probes") for row in records)
        ping_attempts = sum(zaehler(row, "ping_attempts") for row in records)
        ping_failures = sum(zaehler(row, "ping_failures") for row in records)
        summary = {
            "band": band,
            "run_count": len(records),
            "usable_run_count": len(usable),
            "download_median_mbps": statistics.median(downloads) if downloads else None,
            "download_min_mbps": min(downloads) if downloads else None,
            "download_max_mbps": max(downloads) if downloads else None,
            "ping_median_ms": statistics.median(pings) if pings else None,
            "ping_min_ms": min(pings) if pings else None,
            "ping_max_ms": max(pings) if pings else None,
            "jitter_median_ms": statistics.median(jitters) if jitters else None,
            "probe_attempts": probe_attempts,
            "failed_probes": failed_probes,
            "ping_attempts": ping_attempts,
            "ping_failures": ping_failures,
        }
        summary["failure_rate"] = (
            failed_probes / probe_attempts if probe_attempts else 1.0
        )
        summary["ping_failure_rate"] = (
            ping_failures / ping_attempts if ping_attempts else 1.0
        )
        bands.append(summary)

    eligible = [band for band in bands if band["usable_run_count"] >= 1]
    download_best = max(
        eligible,
        key=lambda row: row["download_median_mbps"],
        default=None,
    )
    latency_best = min(
        eligible,
        key=lambda row: (
            row["ping_failure_rate"],
            row["ping_median_ms"],
            row["jitter_median_ms"] if row["jitter_median_ms"] is not None else float("inf"),
        ),
        default=None,
    )
    return {
        "run_count": len(valid_runs),
        "recommendation_run_count": len(recommendation_runs),
        "ignored_run_count": len(valid_runs) - len(recommendation_runs),
        "recommendation_window_days": BAND_RECOMMENDATION_WINDOW_DAYS,
        "recommendation_max_runs": BAND_RECOMMENDATION_MAX_RUNS,
        "minimum_runs": minimum_runs,
        "recommendation_stable": bool(bands) and all(
            band["usable_run_count"] >= minimum_runs for band in bands
        ),
        "bands": bands,
        "download_best": download_best,
        "latency_best": latency_best,
    }
