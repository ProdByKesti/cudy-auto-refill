# Cudy Auto Refill for Android

This is the separate Android app module (`:cudy-app`). The existing `:app` module remains a disposable, read-only background connection probe. Both apps use the reusable `:security-gate` library.

## Current app actions

- Verify the router login and securely save its password.
- Send a manually confirmed `Refill` SMS to `80808` after Android system authentication and a second send confirmation.
- Read the router's available LTE bands and, after system authentication, apply a choice. The app reads the choice back and checks internet access; it restores the previous router selection if the new one does not recover.
- Change the router admin password after system authentication. The app saves the new password only after a fresh login with it succeeds.

Router passwords are encrypted with AES-GCM using a key held by Android Keystore. App backup is disabled. Passwords, tokens, and SMS text are not written to logs. The router's local LuCI interface uses HTTP by default; keep the router address on a trusted local network.

The manual SMS screen reports that the router accepted the send request. It does not claim carrier delivery or perform the desktop app's outbox confirmation. Background monitoring, automatic trigger handling, automatic refills, and their always-available Stop control are not implemented in this module yet. The read-only probe must not be used as the production app.

## Run on the emulator

Open the parent `android-probe-fgs` Gradle project, select the `cudy-app` run configuration (application ID `de.prodbykesti.cudyapp`), and run it on a device or emulator connected to the router's Wi-Fi. In the project tree, `app` is still the probe; choose the separate `cudy-app` configuration to launch this app.

Configure a secure screen lock on the Android device before using any protected action. A device with no secure lock or supported biometric can still open the app and view its screens, but Android will not authorize a sensitive action.

Use **Verify connection and save securely** to confirm the current router username and password before saving. The first manual refill, password change, or LTE band apply requires both Android authentication and the action's confirmation where shown.
