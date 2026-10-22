# ROU-10: Android architecture and APK feasibility

**Assessment date:** 2026-10-22  
**Status:** Feasibility findings recorded; a device prototype is still required.

## Current project

The repository currently contains the Windows desktop application in Python/Tkinter. Its router client uses `requests` and BeautifulSoup, while UI, polling threads, Windows DPAPI credential storage, and runtime state are part of the desktop process. There is no Android/Gradle module yet.

Keep the working desktop application unchanged. Build Android as a separate app/module in this repository. Reuse the behavior and protocol knowledge, not the Tkinter UI or Windows-specific storage code. The Android client will need its own router session, message state, credential storage, and interface.

## Recommended direction

Use a native Android app with Kotlin and Jetpack Compose for the first Android release. Edit its source in VS Code and use the Pixel emulator for device checks. Android Studio is only needed for the already-installed SDK/emulator tools; it does not need to be the main editor.

This choice keeps Android-specific requirements explicit: a user-controlled monitoring service, notification controls, local-network access, and Android secure storage. The planned iOS version is far enough in the future that sharing a cross-platform UI now is not worth making the Android background behavior harder to validate.

## Constraints to validate in a prototype

1. **Router access:** the desktop app currently contacts the router over plain HTTP at `192.168.10.1`. The emulator ADB connection is online (`emulator-5554`), but that alone does not prove the emulator can reach the router. Verify a harmless unauthenticated router-page request from the Android app before implementing login or SMS actions. Android's newer local-network permission behavior also needs to be handled for newer target SDKs.
2. **Continuous monitoring:** a ten-second poll cannot use ordinary periodic WorkManager work; its minimum repeat interval is 15 minutes and its run time is not exact. Android 15 also limits `dataSync` foreground services to six hours per 24 hours. Since this app communicates with a local router, a user-started `connectedDevice` foreground service is the candidate to evaluate; it must show an ongoing notification and be tested with the screen locked and the app backgrounded.
3. **Credential safety:** store the router password using Android Keystore-backed encrypted storage. Never reuse the desktop DPAPI file or commit credentials.
4. **Family distribution:** build a signed APK for direct installation. Preserve and back up the signing key; future APK updates must use the same key. Sideloading and update steps need a short, beginner-friendly guide.
5. **Shared behavior:** first port only safe read-only operations: router reachability, login/session validation, and inbox listing. Keep SMS sending, cleanup, band changes, and speed tests out of the initial connectivity probe.

## Prototype sequence

1. Create a minimal Android app module without changing the desktop entry points.
2. Request only the permissions needed for network access and the monitoring notification.
3. Add a read-only connection check for the router and display reachable/unreachable with a useful error.
4. Verify the check on the Pixel emulator; then verify on a real phone connected to the Cudy Wi-Fi, because emulator routing can differ from a physical device.
5. Add a user-started foreground service prototype and inspect polling/notification behavior after app switching and screen lock. Do not send SMS or alter router settings in this prototype.
6. Produce a locally installable debug APK and document the signed-release and sideload path separately.

## References

- [Android: schedule periodic WorkManager jobs](https://developer.android.com/develop/background-work/background-tasks/persistent/getting-started/define-work)
- [Android: foreground-service timeouts](https://developer.android.com/develop/background-work/services/foreground-services/timeout)
- [Android: data transfer to a local device](https://developer.android.com/develop/background-work/background-tasks/data-transfer-options)
- [Android: local network permission](https://developer.android.com/privacy-and-security/local-network-permission)
