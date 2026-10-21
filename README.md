# Cudy Auto Refill

A small Python/Tkinter desktop tool for a Cudy LT300 V3 LTE router. It monitors incoming Lidl Connect SMS messages and can reply with `Refill` when configured trigger text is detected.

## Current features

- Router login using the Cudy LuCI web interface
- Incoming SMS polling and trigger matching for sender `80808`, including the Lidl Connect Unlimited 1 GB used-up message
- Sender fallback for trigger SMS whose number is missing in the inbox list, with sender verification before replying
- Manual emergency `Refill` SMS to `80808` through the router’s New Message form, with confirmation and outbox verification; live-tested successfully
- Refill reply verification by checking for a new matching item in the outbox
- Per-SMS attempt tracking in a local JSON state file
- Inbox and outbox cleanup when configured message-count thresholds are reached
- Session refresh and watchdog recovery with capped retry backoff
- Manual LTE band selection with internet verification and automatic rollback if the connection does not recover
- OS-level instance lock so a second copy exits before starting another polling or refill loop
- Rotating, timestamped local logs and a redacted diagnostic-copy action
- Correlated refill action entries with a short action ID, separating trigger detection, send decision, router submission, and immediate or delayed outbox confirmation
- Optional log Auto-Follow control, enabled by default; turn it off to read or scroll older log entries without being pulled back to the bottom
- All-band comparison of download throughput, HTTP probe latency, and jitter; the previous band selection is restored after a scan
- One-time speed-test prompt on the first packaged EXE launch, with a repeatable test in the Signal tab

## Requirements

- Python 3
- Tkinter
- Packages listed in `requirements.txt`

Install the Python packages with:

```powershell
python -m pip install -r requirements.txt
```

## Router credentials

For a local Python setup, create `cudy_credentials.json` from the example (for example with `Copy-Item cudy_credentials.example.json cudy_credentials.json`) and enter the router password locally.

On Windows, the app imports that password on first login into `%LOCALAPPDATA%/CudyAutoRefill/router_password.dpapi`, protected with Windows DPAPI for the current Windows user. The source JSON is intentionally left in place; remove it manually after confirming the app can log in. The app also accepts `CUDY_ROUTER_PASSWORD` from the process environment. Never package or commit credentials.

## Run

```powershell
python cudy_auto_refill.py
```

The program only starts monitoring after you press Start in the GUI. It can send SMS replies and delete SMS during cleanup, so review the router address and cleanup thresholds before starting it.

## Working copy

`main_settings_only_fixed2.py` is the original user-supplied version and remains unchanged because it may be running in the background. Continue development in `cudy_auto_refill.py`. Stop the original before running the working copy; both can process the same router inbox. The working copy migrates the old SMS state to the Windows user profile on first use.

## Local data

Runtime state and logs are stored under `%LOCALAPPDATA%/CudyAutoRefill` regardless of the current working directory. On first use, an existing `sms_state.json` beside the working copy is copied there; the original is kept. The state tracks message IDs, content, timestamps, and refill attempts/successes. Do not delete it while trigger SMS messages remain in the inbox, or a message may be treated as unprocessed.

The live GUI log is capped in memory. The rotating log keeps up to four files of about 1 MB each. Refill action entries use short IDs and do not include SMS bodies. The Log tab's Diagnose kopieren action redacts SMS bodies and credential-like values before copying a report.

The Log tab's Auto-Follow control starts enabled and keeps new messages in view. Turn it off to inspect earlier entries; incoming log messages continue to be recorded while the view stays at its current scroll position.

## Current limitations

- Polling for refill triggers reads the first inbox page.
- The band comparison uses Cloudflare's public download endpoint. It transfers up to about 80 MB across ten bands and does not measure upload speed.
- The reported latency and jitter are HTTP probe timings; failed probes are shown as request failures, not a dedicated radio packet-loss measurement.
- Poll interval, session refresh, watchdog, and router URL are stored in the per-user settings file.

The LTE band controls use the LT300 V3 router's Cellular/APN form. The comparison reports the fastest download band and the band with the lowest measured latency and request-failure rate for latency-sensitive use. The manual emergency-send path in the working copy has now been live-tested: the router accepted the SMS and the outgoing `Refill` appeared in the outbox. The automated refill flow has not yet been independently live-tested in this working copy.
