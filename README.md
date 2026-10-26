# Cudy Auto Refill

A small Python/Tkinter desktop tool for a Cudy LT300 V3 LTE router. It monitors incoming Lidl Connect SMS messages and can reply with `Refill` when configured trigger text is detected.

## Current features

- Router login using the Cudy LuCI web interface
- Incoming SMS polling and trigger matching for sender `80808`, including the Lidl Connect Unlimited 1 GB used-up message
- Sender fallback for trigger SMS whose number is missing in the inbox list, with sender verification before replying
- Manual emergency `Refill` SMS to `80808` through the router’s New Message form, with confirmation and outbox verification; live-tested successfully
- A separate Download tab with temporary faster polling/session-refresh/watchdog values, restored when the mode is switched off
- Optional proactive `Refill` SMS cycles in Download mode; off by default, explicitly confirmed, and bounded by a minimum 60-second interval, a 12-hour default runtime limit, and a configurable maximum count
- Proactive cycles share the refill send lock with inbox-triggered and manual sends, wait for an outbox entry, and pause after an unconfirmed result
- Refill reply verification by checking for a new matching item in the outbox
- Per-SMS attempt tracking in a local JSON state file
- Inbox and outbox cleanup when configured message-count thresholds are reached; mailbox pages are scanned at startup and then every five minutes (with a shorter retry after cleanup errors)
- Session refresh and watchdog recovery with capped retry backoff
- Manual LTE band selection with internet verification and automatic rollback if the connection does not recover
- OS-level instance lock so a second copy exits before starting another polling or refill loop
- Rotating, timestamped local logs and a redacted diagnostic-copy action
- Correlated refill action entries with a short action ID, separating trigger detection, send decision, router submission, and immediate or delayed outbox confirmation
- Optional log Auto-Follow control, enabled by default; turn it off to read or scroll older log entries without being pulled back to the bottom
- All-band comparison of download throughput, HTTP probe latency, and jitter; the previous band selection is restored after a scan
- One-time speed-test prompt on the first packaged EXE launch, with a repeatable test in the Signal tab
- Router admin password can be changed from Settings; the app verifies a fresh login before saving the new password with Windows DPAPI
- First-run prompt recommends replacing the factory router password and verifies the password entered for the app
- Settings can be exported and restored as a versioned JSON file; only the router address and monitoring intervals are included

## Android app in development

The separate Android work is in `android-probe-fgs`. It contains `:cudy-app` for the router UI, `:security-gate` for Android system authentication, and `:app` as a read-only background-connection probe. In Android Studio, open `android-probe-fgs` and choose the `cudy-app` run configuration to launch the router app; `app` still launches the probe. The current Android app has protected manual refill, router-password change, and LTE-band selection. Background monitoring and automatic refill are not implemented in Android yet, so the Python desktop app remains the complete version.

## Requirements

- Python 3
- Tkinter
- Packages listed in `requirements.txt`

Install the Python packages with:

```powershell
python -m pip install -r requirements.txt
```

## Windows EXE for family and friends

The Windows release is a standalone EXE; people using it do not need to install Python. Build it on Windows from a checkout of this repository. The build machine needs Python 3 and an internet connection for the isolated build environment and its packages.

From PowerShell in the project folder, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_windows.ps1
```

The first build creates `.build-venv`, installs the pinned build requirements, and writes two files to `dist`:

- `CudyAutoRefill.exe` is the normal app for family and friends.
- `CudyAutoRefill_FirstStart.exe` is a repeatable setup and recovery simulator for development. It uses the current application code, an isolated test profile, and simulated router actions; it does not send SMS, change LTE bands, or clean router messages.

Build output and the virtual environment are ignored by Git. Publish only the normal app as an asset on a GitHub Release; do not commit either EXE into the source repository.

On first launch, the app guides the user through the router address and SMS polling interval, checks that the router web page can be reached, and then offers the router-password security setup. It recommends the all-band LTE comparison once; this can also be run later from the Signal tab. The PC must be connected to the Cudy router over Wi-Fi or LAN during setup. Monitoring only begins after the user presses Start.

### Updating and recovery

To update, close the app, download the newer EXE from the GitHub Release, and replace the old EXE. Keep it under the same Windows user account so the saved password can still be decrypted. Router settings, the DPAPI-protected password, SMS state, LTE profile, and logs stay in `%LOCALAPPDATA%\CudyAutoRefill`, separate from the EXE and the public project folder. Replacing the EXE does not remove those files.

If an update causes a problem, close the app and replace the EXE with the previous GitHub Release. For support, copy the redacted report from the Log tab before changing or deleting local data. To completely reset the app for the current Windows user, first close it, then rename `%LOCALAPPDATA%\CudyAutoRefill` to keep a backup; the next launch starts a fresh setup. The saved router password is protected for the Windows user and should not be copied into a public repository or shared with another person.

### First-launch checklist

Use a fresh Windows user profile or a clean Windows test machine to check a release:

1. Confirm that the EXE starts without Python installed.
2. Confirm that router address and polling interval can be saved and that the router connection check reports the correct result.
3. Confirm that the password setup and optional LTE band comparison behave as expected.
4. Close and reopen the EXE; the initial setup prompts should not repeat, and the saved settings should remain available.
5. Confirm that no password, SMS history, settings, or log files appear beside the EXE.

## Router credentials

For a local Python setup, create `cudy_credentials.json` from the example (for example with `Copy-Item cudy_credentials.example.json cudy_credentials.json`) and enter the router password locally.

On Windows, the app imports that password on first login into `%LOCALAPPDATA%/CudyAutoRefill/router_password.dpapi`, protected with Windows DPAPI for the current Windows user. The source JSON is intentionally left in place; remove it manually after confirming the app can log in. The app also accepts `CUDY_ROUTER_PASSWORD` from the process environment. Never package or commit credentials.

The Settings tab can change the router's admin password or verify and securely store a password that was changed in the router web interface. Password changes are sent using the router's password form and are saved in the app only after a fresh login with the new password succeeds. Stop SMS monitoring and LTE tests before changing the router password. The first-run prompt recommends changing the factory password; the router does not expose a reliable signal for the app to identify whether it is still using the factory password.

## Run

```powershell
python cudy_auto_refill.py
```

To repeat the onboarding flow on every launch, run the separate first-start test launcher:

```powershell
python cudy_auto_refill_first_start.py
```

The build script also creates `dist\CudyAutoRefill_FirstStart.exe`, which runs the same first-start test launcher without requiring Python on the test computer.

It uses an isolated test profile and ignores saved passwords and SMS history. On each launch, choose one of four repeatable scenarios: successful connection, one failed login followed by success, an unavailable router with three retries, or a rejected router password. The normal router-password onboarding and LTE prompt then run again.

| First-start scenario | Expected result |
| --- | --- |
| Successful connection | Simulated login succeeds; a sender-less sample trigger is handled and a simulated outbox entry confirms the refill. |
| Retry once | The first login fails and the next simulated attempt succeeds. |
| Router unavailable | Three simulated login attempts fail and the login error remains visible. |
| Password rejected | The password dialog remains open and reports the simulated rejection. |

In this mode, router access, LTE band changes, SMS sending, refill actions, and SMS cleanup are blocked or simulated. A sample trigger SMS with a missing sender and its outbox confirmation are simulated locally. No credentials, settings, LTE profile, or SMS state are written. The launcher runs the current `cudy_auto_refill.py`, so feature updates stay in sync without maintaining a second copy of the application. It shares the app's instance lock; close the normal app before starting it.

The Download tab is available in both launch paths. In the first-start test launcher, refills remain simulated. In normal mode, Download mode can be switched on or off without restarting the app. Proactive refill SMS require a separate confirmation and are disabled by default. The interval, runtime, and count can be limited in the tab; defaults are 60 seconds, 12 hours, and 20 SMS. A missing outbox confirmation pauses further proactive sends. An outbox entry confirms that the router queued the SMS; it does not prove that Lidl Connect activated another data allowance.

In normal mode, monitoring starts only after you press Start in the GUI. It can send SMS replies and delete SMS during cleanup, so review the router address and cleanup thresholds before starting it. The first-start test launcher blocks those actions.

## Working copy

`main_settings_only_fixed2.py` is the original user-supplied version and remains unchanged because it may be running in the background. Continue development in `cudy_auto_refill.py`. Stop the original before running the working copy; both can process the same router inbox. The working copy migrates the old SMS state to the Windows user profile on first use.

## Local data

Runtime state and logs are stored under `%LOCALAPPDATA%/CudyAutoRefill` regardless of the current working directory. On first use, an existing `sms_state.json` beside the working copy is copied there; the original is kept. The state tracks message IDs, content, timestamps, and refill attempts/successes. Do not delete it while trigger SMS messages remain in the inbox, or a message may be treated as unprocessed.

The live GUI log is capped in memory. The rotating log keeps up to four files of about 1 MB each. Refill action entries use short IDs and do not include SMS bodies. The Log tab's Diagnose kopieren action redacts SMS bodies and credential-like values before copying a report.

The Log tab's Auto-Follow control starts enabled and keeps new messages in view. Turn it off to inspect earlier entries; incoming log messages continue to be recorded while the view stays at its current scroll position.

## Current limitations

- Polling for refill triggers reads the first inbox page.
- Download-mode proactive refills verify the router outbox only; they cannot confirm that the carrier activated data or that a long download resumed.
- The band comparison uses Cloudflare's public download endpoint. It transfers up to about 80 MB across ten bands and does not measure upload speed.
- The reported latency and jitter are HTTP probe timings; failed probes are shown as request failures, not a dedicated radio packet-loss measurement.
- Poll interval, session refresh, watchdog, and router URL are stored in the per-user settings file. The Settings tab can export and restore these four values; imports are validated and shown for confirmation before they are applied. Passwords, tokens, SMS content, SMS history, and the first-run prompt state are excluded.

The LTE band controls use the LT300 V3 router's Cellular/APN form. The comparison reports the fastest download band and the band with the lowest measured latency and request-failure rate for latency-sensitive use. The manual emergency-send path in the working copy has now been live-tested: the router accepted the SMS and the outgoing `Refill` appeared in the outbox. The automated refill flow has not yet been independently live-tested in this working copy.
