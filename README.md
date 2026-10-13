# Cudy Auto Refill

A small Python/Tkinter desktop tool for a Cudy LT300 V3 LTE router. It monitors incoming Lidl Connect SMS messages and can reply with `Refill` when configured trigger text is detected.

## Current features

- Router login using the Cudy LuCI web interface
- Incoming SMS polling and trigger matching for sender `80808`
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
python main_settings_only_fixed2.py
```

The program only starts monitoring after you press Start in the GUI. It can send SMS replies and delete SMS during cleanup, so review the router address and cleanup thresholds before starting it.

## Local data

`sms_state.json` tracks message IDs, content, timestamps, and refill attempts/successes. The program creates or updates this file in its current working directory. If it is missing, an existing trigger SMS may be treated as unprocessed and cause another refill attempt. Keep this file local and do not delete it while trigger SMS messages remain in the inbox.

## Current limitations

- Polling for refill triggers reads the first inbox page.
- Signal reading and LTE band management are not connected to the router API yet.
- The manual test-refill button is a placeholder.
- Polling and watchdog settings apply at runtime and are not persisted between launches.

This repository starts from the selected working-file baseline. The prior working behavior was reported by the project owner; the current version has not been live-tested as part of this repository setup.
