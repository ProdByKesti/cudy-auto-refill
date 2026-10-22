# ROU-10 – Android feasibility: emulator check

**Date:** 2026-10-22
**Status:** In progress

## Confirmed

- Android Studio's emulator is attached to ADB as `emulator-5554` (`device`). A second emulator instance was listed as `offline`; it is not needed for this check.
- The emulator's Chrome browser opens `http://192.168.10.1` and displays the Cudy router's normal login page.
- This confirms the emulator can reach the router over the current local Wi-Fi/network setup. No login was submitted and no router setting was changed.

## What this does and does not prove

This is a browser-level connectivity check. It does **not** yet prove that an Android app can make the same HTTP request, nor that Android will keep polling while the app is backgrounded or the display is locked.

## Android probe prepared

The separate `android-probe` project now contains a small Android-native app with two actions:

- A one-time, unauthenticated HTTP `GET` to the router root, showing only the status code and elapsed time.
- A one-minute check that repeats the same read-only request every five seconds. This lets us compare results before and after switching apps or locking the emulator.

The probe does not submit credentials, inspect SMS, or change router settings. It is separate from the working Python desktop app. The user confirmed the app launched and the one-minute emulator monitor ran.

After the first user run showed a slow or failed attempt, the monitor was updated to retain and display all twelve request results plus a connection-error count. Re-run the app after Android Studio rebuilds it to capture the exact failure instead of losing it when the next poll updates the screen.

## Next validation

Open `android-probe` in Android Studio, let Gradle sync, and run the app on the Pixel emulator. First confirm the one-time request returns an HTTP status. Then run the one-minute check, switch apps or lock the display for 20–30 seconds, return, and note the completed-request count. This short probe does not promise reliable continuous background polling; it tells us what this emulator permits without a foreground service.

## Architecture direction

- Keep the existing Python/Tkinter desktop program as-is.
- Build the mobile UI and Android lifecycle handling natively for Android; share the protocol behavior and test cases conceptually, rather than trying to run Tkinter on Android.
- Treat continuous, near-real-time SMS polling as a separate Android lifecycle design decision. A successful browser request alone does not validate that behavior.
- For the first private family/friends release, distribute a signed APK directly and document Android's install-from-this-source step. Revisit store publication later.

## Emulator run update

The user clarifies that they switched to the Phone app while leaving the display on. The probe's background check effectively stopped; two successful entries appeared when they briefly returned to it. This isolates the observed issue to app-switch/background behavior rather than screen lock. The current version uses only an Activity-owned thread, not a foreground service, so it is not a valid design for reliable background polling.

The next prototype step is a user-started Android foreground service with an ongoing notification and the same limited, read-only one-minute check. This tests the intended Android mechanism while keeping the probe's network behavior unchanged.
