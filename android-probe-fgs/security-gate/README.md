# Sensitive Action Gate

Reusable Android system-authentication component for ROU-16. It requests strong biometric or secure device-credential confirmation before one named action and reports success only for that action.

## Integration contract

- Add this module as a dependency of the Android app.
- Host it from a `FragmentActivity` and implement `SensitiveActionGate.Listener`.
- Call `request(Action)` immediately before the sensitive operation.
- Run the operation only inside `onAuthorized(Action)` and only for the matching action. Do not keep an unlocked flag or cache authorization.
- Forward `saveState(Bundle)` from the activity's `onSaveInstanceState` and route the result of request code `4102` to `onActivityResult`.
- Keep status, logs, and the action that stops background monitoring outside the gate.
- Treat cancellation, unavailable device authentication, and any non-success result as no authorization.

The module supports `minSdk 23`. Android 11 and newer can show biometric and device credential in the same system prompt. Android 10 and older use a strong-biometric prompt with a device-credential fallback.

The `:app` probe remains a read-only consumer example. The separate `:cudy-app` module wires the gate to manual refill, password change, and LTE band apply. Android background monitoring and automatic refill integration, plus device validation, are still required before ROU-16 is complete.
