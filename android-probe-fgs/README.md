# Cudy foreground connection probe

This is a disposable Android test app for checking whether read-only requests to the Cudy router continue while another app (for example, Phone) is in front.

- Target: `http://192.168.10.1/`
- Sends only HTTP GET requests to the router login page
- Does not log in, change router configuration, or send SMS
- A user-started foreground service performs 12 checks, 5 seconds apart
- An ongoing notification shows progress and provides a Stop action
- Results are kept locally so they can be read after returning to the app

Import this directory in Android Studio and run the `app` configuration on the emulator. Grant notification permission when asked, tap **Start 12 background checks**, switch to the Phone app for about one minute, then return to read the complete result list.
