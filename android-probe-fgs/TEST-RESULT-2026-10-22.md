# Android background probe result — 2026-10-22

## Result

The user reports two successful emulator runs of the foreground-service probe:

- All 12 checks succeeded while switching from the probe app to the Android Phone app, with the display on.
- All 12 checks succeeded during a properly performed screen-off test.

## What this confirms

- The router was reachable from the emulator during this run.
- The user-started foreground service continued its short polling run while another app was in front.
- The service also completed all checks with the emulator display off.
- Neither run had a reported failed check.

## Limits

- These were short emulator tests of roughly one minute each.
- It does not establish long-term behavior during an overnight run, after reboot, or under a physical phone vendor's battery management.
- No physical Android phone is available for this project test.

This is an Android architecture probe for ROU-10, not the production Android app.
