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
- Session refresh and watchdog recovery
- Manual selection of the LTE bands exposed by the router
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

Create `cudy_credentials.json` from the example and enter the router password locally:

```powershell
Copy-Item cudy_credentials.example.json cudy_credentials.json
```

The credentials file is excluded by `.gitignore`. You can instead set `CUDY_ROUTER_PASSWORD` in the process environment. Never commit router credentials.

## Run

```powershell
python cudy_auto_refill.py
```

The program only starts monitoring after you press Start in the GUI. It can send SMS replies and delete SMS during cleanup, so review the router address and cleanup thresholds before starting it.

## Working copy

`main_settings_only_fixed2.py` is the original user-supplied version and remains unchanged because it may be running in the background. Continue development in `cudy_auto_refill.py`, which starts as an identical copy. Do not run it alongside the original; both versions can process the same router inbox and share `sms_state.json`.

## Local data

`sms_state.json` tracks message IDs, content, timestamps, and refill attempts/successes. The program creates or updates this file in its current working directory. If it is missing, an existing trigger SMS may be treated as unprocessed and cause another refill attempt. Keep this file local and do not delete it while trigger SMS messages remain in the inbox.

## Current limitations

- Polling for refill triggers reads the first inbox page.
- The band comparison uses Cloudflare's public download endpoint. It transfers up to about 80 MB across ten bands and does not measure upload speed.
- The reported latency and jitter are HTTP probe timings; failed probes are shown as request failures, not a dedicated radio packet-loss measurement.
- Polling and watchdog settings apply at runtime and are not persisted between launches.

The LTE band controls use the LT300 V3 router's Cellular/APN form. The comparison reports the fastest download band and the band with the lowest measured latency and request-failure rate for latency-sensitive use. The manual emergency-send path in the working copy has now been live-tested: the router accepted the SMS and the outgoing `Refill` appeared in the outbox. The automated refill flow has not yet been independently live-tested in this working copy.
