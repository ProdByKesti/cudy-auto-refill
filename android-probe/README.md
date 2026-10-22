# Cudy Router Probe (ROU-10)

Small Android-only, read-only prototype for checking router access and short background behavior. It is separate from the desktop Python app.

## What it does

- Sends an unauthenticated HTTP `GET` to `http://192.168.10.1/` and displays only the HTTP status and elapsed time.
- Offers a one-minute monitor that repeats the same read-only request every five seconds. While it runs, switch to another app or lock the emulator, then return and check how many requests completed.
- Does not submit a password, inspect SMS, change router settings, or store credentials.

## Open and run

1. In Android Studio, open the `android-probe` folder as a separate project.
2. Let Gradle sync and install any SDK component Android Studio requests.
3. Select the running Pixel 8a emulator and run the `app` configuration.
4. Tap **Check router connection**. An HTTP status confirms the native app reached the router.
5. Tap **Start 60-second background check**, switch to another app or lock the emulator for 20–30 seconds, then return and note the completed request count.

The current emulator browser result is a separate browser-level check; this prototype is meant to verify app-level access. Android may stop or delay this prototype while it is backgrounded. That is useful evidence for ROU-10 and is not a promise of continuous polling.

## Compatibility note

This proof app allows cleartext HTTP because the router login page is served over local HTTP. Keep it limited to the fixed router address and never send credentials through this probe. A production app needs a deliberate local-network permission and transport-security design for its target Android version.
