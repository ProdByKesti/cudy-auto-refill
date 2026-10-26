# Cudy foreground connection probe

This is a disposable Android test app for checking whether read-only requests to the Cudy router continue while another app (for example, Phone) is in front.

- Target: `http://192.168.10.1/`
- Sends only HTTP GET requests to the router login page
- Does not log in, change router configuration, or send SMS
- A user-started foreground service performs 12 checks, 5 seconds apart
- An ongoing notification shows progress and provides a Stop action
- Results are kept locally so they can be read after returning to the app

Import this directory in Android Studio and run the `app` configuration on the emulator. Grant notification permission when asked, tap **Start 12 background checks**, switch to the Phone app for about one minute, then return to read the complete result list.

## ROU-16 authentication and app modules

This project contains three separate modules: `:app` is the read-only background connection probe, `:security-gate` is the reusable Android system-authentication library, and `:cudy-app` is the separate Android router app. Do not use the probe run configuration to evaluate the production controls.

The probe's **Demo** buttons still show only a local “authorized” result. The `:cudy-app` module connects the gate to its real manual refill, router password update, and LTE-band apply actions. Each action prompts again; there is no app PIN and no reusable unlocked state. Status and read-only band loading are available without authentication. Canceling the system prompt authorizes nothing.

To try the gate, configure a screen lock on the emulator in Android **Settings → Security & privacy → Device unlock**. Run the `cudy-app` configuration to reach the real router controls, or run `app` to keep exercising the read-only probe. If the emulator has a strong biometric enrolled, Android may offer it; otherwise use the device PIN, pattern, or password. The Android app still needs background monitoring and automatic-refill integration, followed by emulator and real-device validation, before ROU-16 can be completed.
