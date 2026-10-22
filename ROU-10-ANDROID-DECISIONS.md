# ROU-10 — Android architecture and distribution findings

Updated: 2026-10-22

## Validation results

- **Router access from the emulator:** confirmed in Chrome at `http://192.168.10.1/` and from the probe's read-only HTTP GET. The probe does not log in or modify router settings.
- **App switch:** passed in the emulator. The user-started foreground-service probe completed all 12 checks while the Phone app was in front and the display remained on.
- **Display lock / screen off:** passed in the emulator. The user reports all 12 checks succeeded with the display off. No physical Android phone is available, so OEM-specific battery restrictions cannot be validated in this task.
- **Foreground-service model:** a persistent, user-started monitor should have an explicit status notification and a Stop action. It must be started while the app is visible. Android 14+ requires a declared foreground-service type and matching permission. `connectedDevice` is the relevant candidate for a local network device such as the Cudy router; validate on supported Android versions before production.

## Android UI versus reusable router behavior

- The existing desktop UI is Tkinter and cannot be reused as a native Android interface.
- Android needs a new native UI and Android lifecycle/service layer.
- Reuse the router protocol knowledge, endpoint/form behavior, trigger text rules, refill state transitions, and safe SMS fixtures as specifications and shared test data.
- Implement the Android network/session client in Kotlin or Java and verify its token, cookie, and CSRF behavior against the router. Do not assume that the desktop Python HTTP flow can be copied directly.

## Family and friends distribution proposal

- For a private Android release, build a **signed APK** and publish it as a GitHub Release asset. A public release download does not require family members to sign into GitHub.
- On Android 8+, the user must allow installs from the specific source (for example, the browser or file manager) before installing an APK downloaded outside an app store.
- Updates must keep the same application ID and signing certificate and increase the version code. Keep the private signing key backed up securely and out of Git; losing it prevents normal in-place updates.
- Android's developer-verification rollout is evolving. As of 2026-10-22, official guidance describes a limited-distribution path for hobbyists/students for up to 20 devices without an account fee, with the initial September 2026 enforcement applying only to selected countries and broader rollout planned for 2027. Confirm the current Germany-specific requirement before release.

## Scope conclusion

ROU-10's architecture and emulator validation criteria are complete. It does not require a production Android app or a physical phone. Physical-device battery behavior remains a known limitation for later family beta testing.

## Official references

- [Android background execution and foreground-service options](https://developer.android.com/develop/background-work/background-tasks/data-transfer-options)
- [Foreground-service type requirements](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [APK installation outside Google Play](https://developer.android.com/distribute/marketing-tools/alternative-distribution)
- [App signing and update compatibility](https://developer.android.com/studio/publish/app-signing)
- [Android developer verification and limited distribution](https://developer.android.com/developer-verification/guides)
