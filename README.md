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
- GUI controls for router URL and runtime polling intervals

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
- Signal reading and LTE band management are not connected to the router API yet.
- Polling and watchdog settings apply at runtime and are not persisted between launches.

The original version's working behavior was reported by the project owner. The manual emergency-send path in the working copy has now been live-tested: the router accepted the SMS and the outgoing `Refill` appeared in the outbox. The automated refill flow has not yet been independently live-tested in this working copy.
