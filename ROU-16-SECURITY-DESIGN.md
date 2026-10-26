# ROU-16 – Security design for sensitive app actions

**Decision date:** 2026-10-26  
**Scope:** Android app modules. The disposable probes remain read-only; a separate `:cudy-app` module now contains authenticated router controls.

## Recommendation

Use Android's system authentication prompt for sensitive actions, accepting a strong biometric or the device screen-lock credential (PIN, pattern, or password). Do not create a separate Cudy app PIN for the first family release.

This gives the user a familiar system dialog, avoids storing or recovering a second app-specific PIN, and supports phones without biometric hardware as long as a secure screen lock is configured. Check authenticator availability at setup and use an API-compatible flow for the app's eventual minimum Android version. Android documents that some `DEVICE_CREDENTIAL` combinations are unsupported on Android 10 and earlier, so the production implementation must handle those versions explicitly.

If the device has no supported biometric or secure screen lock, keep status and read-only information available, but do not allow sensitive actions or enable automatic refill until the user configures a supported screen lock.

## Alternatives considered

| Option | Benefits | Costs and risks | Decision |
|---|---|---|---|
| App-specific PIN | Works independently of the phone's lock screen; app controls the prompt | Requires secure verifier storage, attempt throttling, reset/recovery behavior, and another secret the family can forget; a weak PIN can be guessed | Do not add for the first release |
| Android system authentication | Familiar OS-owned prompt; can accept strong biometrics and the device PIN/pattern/password; the app does not manage a second PIN | Requires a secure device credential; API combinations need compatibility handling; any credential enrolled on the device may authorize the action | Recommended |

This is local device authorization, not individual family-member identity. Anyone whose biometric or screen-lock credential is accepted by Android can authorize these actions on that phone.

## Action policy

| App area or action | Required protection |
|---|---|
| Status, connection state, and read-only logs | No prompt; keep these immediately accessible |
| Manual refill SMS | System authentication immediately before sending, followed by a clear send confirmation. Canceling either step sends nothing. |
| Change router credentials | System authentication immediately before saving. Keep the router's own password-change validation in place. |
| Apply LTE band selection | System authentication immediately before applying the selection. A failed router verification must still trigger the existing rollback behavior. |
| Change or enable automatic refill settings | System authentication when applying the settings. The displayed summary must state what will run automatically. |
| Stop monitoring or disable automatic actions | Always allow an immediate stop without authentication; stopping is a safe action and must remain available as an emergency control. |

An authentication grant is one-use and belongs to the action that requested it. Do not keep a general “unlocked” flag. Discard an unused grant when the user cancels, leaves the sensitive screen, the app goes to the background, or the device locks. A batch of settings can use one prompt when the user presses its single Apply button.

## Background monitoring and automatic refills

The app lock protects interactive controls only. It must not pause or terminate a user-started foreground service, scheduled checks, or already-enabled automatic refill rules when the UI is locked or backgrounded. Android foreground services are separate from the activity UI and remain visible through their required notification.

Require authentication when the user enables or changes an automatic rule in the foreground. Treat that explicit Apply action as authorization for future runs of that saved rule. The background service must not display an authentication prompt or wait for the UI. Keep an always-available Stop action in the app and service notification.

Router credentials needed for background operation must be stored encrypted with a key protected by Android Keystore. Do not make routine background credential use depend on an interactive, per-use biometric prompt; that would prevent autonomous checks while the app UI is unavailable. The interactive prompt authorizes configuration and manual actions, while encrypted storage protects credentials at rest.

## Lockout and recovery behavior

- If the user cancels the system prompt or authentication fails, cancel that action and leave settings unchanged. The user can retry through Android's system prompt, including its device-credential fallback.
- If no supported device authentication is configured, explain how to set a screen lock. Keep read-only status available; leave sensitive controls and automatic-refill activation unavailable until setup is complete.
- The app must not offer a hidden PIN bypass or an app-side reset that weakens the device lock. Forgotten device credentials are handled through the device owner's normal Android recovery options; exact recovery depends on the phone and account.
- After reinstall or a phone change, require authentication setup again. Do not restore an old authorization grant. If the Android Keystore key protecting the router password is unavailable, ask the user to enter the router password again instead of silently falling back to plaintext.
- Do not include router passwords, authentication grants, session tokens, or SMS contents in diagnostics, settings export, or backup data.

## Implementation boundary and prototype

The disposable ROU-10 probes remain read-only with respect to the router. `android-probe-fgs` contains the reusable `:security-gate` library, the read-only probe app in `:app`, and a separate `:cudy-app` module. The production module now connects system authentication to manual refill, router password update, and LTE band apply operations. Passwords are encrypted with an Android Keystore AES-GCM key and are saved only after a verified router login. Manual refill has an additional send confirmation. Band changes are read back from the router and roll back to the previous choice if internet does not return. The manual SMS operation currently reports router acceptance; carrier delivery and outbox confirmation are not yet implemented in Android.

The production app now routes manual refill, router-password changes, LTE-band changes, and enabling automatic monitoring through `SensitiveActionGate`. Status and logs remain available without authentication, while stopping monitoring stays available without a prompt. The foreground service continues Inbox polling independently of the activity; the 2026-10-26 emulator diagnostics show successful checks every 10–11 seconds and a successful initial baseline.

The emulator also confirmed that an ongoing monitoring notification exists and that notification permission is allowed. Its original low-importance channel placed it in the silent notification section, which made it easy to miss. The current build uses a new quiet, visible channel and includes the latest Inbox-check time. That revised notification still needs an on-emulator runtime check after installation.

ROU-16 remains In Progress until the new notification is verified and the documented cancellation, missing-device-credential, rotation/reinstall, and monitoring-continuity scenarios are checked. The planned real-phone verification is still outstanding because no Android phone is currently available.

## Android references

- [Show a biometric authentication dialog](https://developer.android.com/identity/sign-in/biometric-auth)
- [Android Keystore system](https://developer.android.com/privacy-and-security/keystore)
- [Services overview](https://developer.android.com/develop/background-work/services)
