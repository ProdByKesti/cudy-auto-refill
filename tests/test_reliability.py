import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from html import escape as escape_html
from unittest.mock import Mock, patch

import cudy_auto_refill as app
import lte_features


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class DPAPITests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is only available on Windows")
    def test_test_string_round_trip(self):
        sample = b"disposable-cudy-test-value"
        protected = app._dpapi_transform(sample, protect=True)
        restored = app._dpapi_transform(protected, protect=False)
        self.assertNotEqual(protected, sample)
        self.assertEqual(restored, sample)


class RouterLoginTests(unittest.TestCase):
    def setUp(self):
        self.login_page = (
            '<input name="_csrf" value="csrf">'
            '<input name="salt" value="salt">'
            '<input name="luci_username" value="admin">'
        )

    def test_http_200_is_accepted_when_protected_sms_page_is_open(self):
        router = Mock()
        router.get.side_effect = [
            FakeResponse(200, self.login_page),
            FakeResponse(200, "<html><body>SMS</body></html>"),
        ]
        router.post.return_value = FakeResponse(200, "")
        with redirect_stdout(io.StringIO()):
            with (
                patch.object(app, "session", router),
                patch.object(app, "PASSWORT", "test-password"),
                patch.object(app, "hole_token", return_value="test-token"),
                patch.object(app, "gui_status"),
            ):
                self.assertTrue(app.login())

    def test_http_200_login_form_is_still_rejected(self):
        router = Mock()
        router.get.side_effect = [
            FakeResponse(200, self.login_page),
            FakeResponse(200, '<input name="luci_password">'),
        ]
        router.post.return_value = FakeResponse(200, "")
        with redirect_stdout(io.StringIO()):
            with (
                patch.object(app, "session", router),
                patch.object(app, "PASSWORT", "test-password"),
                patch.object(app, "hole_token", return_value="test-token"),
                patch.object(app, "gui_status"),
            ):
                self.assertFalse(app.login())


    def test_sms_poll_treats_http_200_login_page_as_expired_session(self):
        router = Mock()
        router.get.return_value = FakeResponse(200, '<input name="luci_password">')
        with patch.object(app, "session", router):
            with self.assertRaises(PermissionError):
                app.hole_sms_seite("rec", 1)


class SMSFixtureRegressionTests(unittest.TestCase):
    fixture_path = Path(__file__).parent / "fixtures" / "sms_variants.json"

    def test_refill_outbox_text_accepts_duplicated_tooltip_text(self):
        self.assertTrue(app.ist_refill_outbox_text("Refill"))
        self.assertTrue(app.ist_refill_outbox_text("Refill Refill"))
        self.assertTrue(app.ist_refill_outbox_text("  REFILL   REFILL  "))

        self.assertFalse(app.ist_refill_outbox_text("Please Refill"))
        self.assertFalse(app.ist_refill_outbox_text("Refill later"))
        self.assertFalse(app.ist_refill_outbox_text(""))

    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(cls.fixture_path.read_text(encoding="utf-8"))
        cls.messages = {item["case"]: item for item in cls.fixtures["messages"]}

    @staticmethod
    def inbox_html(messages):
        rows = []
        for index, message in enumerate(messages, start=1):
            rows.append(
                "<tr>"
                f'<td id="fixture-{index}-phone"><p>{escape_html(message["sender"])}</p></td>'
                f'<td id="fixture-{index}-content"><p title="{escape_html(message["content"], quote=True)}">'
                f'{escape_html(message["preview"])}</p></td>'
                f'<td id="fixture-{index}-timestamp"><p>{escape_html(message["timestamp"])}</p></td>'
                f'<td><button name="fixture-{index}.readsms" '
                f'onclick="open(\'cfg={escape_html(message["cfg"], quote=True)}\')">More</button></td>'
                "</tr>"
            )
        return "<table>" + "".join(rows) + "</table>"

    def test_fixture_variants_match_and_parser_deduplicates_rows(self):
        fast = self.messages["fast_consumed"]
        depleted = self.messages["depleted_missing_sender"]
        html = self.inbox_html([fast, fast, depleted])

        messages = app.lese_sms(html)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], fast["content"])
        self.assertTrue(app.ist_refill_trigger(messages[0]["content"]))
        self.assertEqual(messages[1]["phone"], "")
        self.assertEqual(messages[1]["content"], depleted["content"].strip())
        self.assertTrue(app.ist_refill_trigger("  " + depleted["content"]))

    def test_missing_sender_still_selects_refill_without_sending_real_sms(self):
        message = self.messages["depleted_missing_sender"]
        html = self.inbox_html([message])
        parsed = app.lese_sms(html)
        self.assertEqual(parsed[0]["phone"], "")

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(app, "APP_DATA_DIR", directory),
                patch.object(app, "SMS_STATE_FILE", str(Path(directory) / "sms_state.json")),
                patch.object(
                    app, "LEGACY_SMS_STATE_FILE",
                    str(Path(directory) / "missing_legacy_state.json"),
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    with (
                        patch.object(app.session, "request", side_effect=AssertionError("Network disabled in fixture tests")) as network_request,
                        patch.object(app, "refill_versuch", return_value=True) as refill,
                        patch.object(app, "sende_reply") as send_reply,
                        patch.object(app, "sende_neue_sms") as send_new_sms,
                    ):
                        protected = app.verarbeite_sms_liste(parsed, html)

            refill.assert_called_once_with(message["cfg"])
            send_reply.assert_not_called()
            send_new_sms.assert_not_called()
            network_request.assert_not_called()
            self.assertIn(app.sms_id(parsed[0]), protected)

    def test_duplicate_rows_and_confirmed_sms_only_select_one_refill(self):
        message = self.messages["fast_consumed"]
        html = self.inbox_html([message, message])
        parsed = app.lese_sms(html)
        self.assertEqual(len(parsed), 1)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(app, "APP_DATA_DIR", directory),
                patch.object(app, "SMS_STATE_FILE", str(Path(directory) / "sms_state.json")),
                patch.object(
                    app, "LEGACY_SMS_STATE_FILE",
                    str(Path(directory) / "missing_legacy_state.json"),
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    with (
                        patch.object(app.session, "request", side_effect=AssertionError("Network disabled in fixture tests")) as network_request,
                        patch.object(app, "refill_versuch", return_value=True) as refill,
                        patch.object(app, "sende_reply") as send_reply,
                        patch.object(app, "sende_neue_sms") as send_new_sms,
                    ):
                        app.verarbeite_sms_liste(parsed, html)
                        app.verarbeite_sms_liste(parsed, html)

            refill.assert_called_once_with(message["cfg"])
            send_reply.assert_not_called()
            send_new_sms.assert_not_called()
            network_request.assert_not_called()



    def reset_outbox_state(self):
        with app.AKTIVE_FEHLER_LOCK:
            app.AKTIVE_FEHLER.clear()
            app.AKTIVE_FEHLER_TYPEN.clear()
        app.status_data["fehler"] = ""
        with app.OUTBOX_PENDING_LOCK:
            app.OUTBOX_PENDING_BASELINE = None
            app.OUTBOX_PENDING_EXPIRES_AT = 0.0
            app.OUTBOX_PENDING_NEXT_CHECK = 0.0

    def test_http_200_reply_without_outbox_proof_keeps_alert_visible(self):
        self.reset_outbox_state()
        self.addCleanup(self.reset_outbox_state)

        with redirect_stdout(io.StringIO()):
            with (
                patch.object(app, "outbox_snapshot", side_effect=[set(), set(), set(), set()]),
                patch.object(app, "sende_reply", return_value=True) as send_reply,
                patch.object(app.time, "sleep") as sleep,
                patch.object(
                    app.session, "request",
                    side_effect=AssertionError("Network disabled in fixture tests"),
                ) as network_request,
            ):
                confirmed = app.refill_versuch("fixture-cfg-fast")

        self.assertFalse(confirmed)
        send_reply.assert_called_once_with("fixture-cfg-fast", app.REFILL_TEXT)
        self.assertEqual(sleep.call_count, len(app.OUTBOX_CONFIRMATION_DELAYS))
        network_request.assert_not_called()
        self.assertIn("outbox", app.AKTIVE_FEHLER)
        self.assertTrue(app.status_data["fehler"])

    def test_only_a_new_matching_outbox_refill_clears_confirmation_alert(self):
        self.reset_outbox_state()
        self.addCleanup(self.reset_outbox_state)

        old_message = dict(self.messages["outbox_refill_old"])
        old_message["phone"] = old_message["sender"]
        new_message = dict(
            old_message,
            timestamp="FIXTURE_OUTBOX_NEW",
            cfg="fixture-cfg-outbox-new",
        )
        baseline = {app.sms_id(old_message)}
        app.merke_ausstehende_outbox_bestaetigung(baseline)
        app.melde_fehler(
            "outbox",
            "Fixture: Refill noch nicht in der Outbox bestätigt.",
            art="confirmation",
        )

        old_only_html = self.inbox_html([old_message])
        with redirect_stdout(io.StringIO()):
            with (
                patch.object(
                    app, "hole_alle_sms_seiten",
                    return_value={"fixture-page": old_only_html},
                ),
                patch.object(
                    app.session, "request",
                    side_effect=AssertionError("Network disabled in fixture tests"),
                ) as network_request,
            ):
                old_snapshot = app.outbox_snapshot()

        self.assertEqual(old_snapshot, baseline)
        self.assertTrue(app.status_data["fehler"])

        old_and_new_html = self.inbox_html([old_message, new_message])
        with redirect_stdout(io.StringIO()):
            with (
                patch.object(
                    app, "hole_alle_sms_seiten",
                    return_value={"fixture-page": old_and_new_html},
                ),
                patch.object(
                    app.session, "request",
                    side_effect=AssertionError("Network disabled in fixture tests"),
                ) as second_network_request,
            ):
                new_snapshot = app.outbox_snapshot()

        self.assertEqual(new_snapshot, {app.sms_id(old_message), app.sms_id(new_message)})
        self.assertEqual(app.status_data["fehler"], "")
        self.assertNotIn("outbox", app.AKTIVE_FEHLER)
        network_request.assert_not_called()
        second_network_request.assert_not_called()



class BandFallbackTests(unittest.TestCase):
    previous = {"automatic": False, "selected": ["3"]}

    def call_helper(self):
        return lte_features.apply_band_config_with_fallback(
            Mock(), "http://router", lambda: True, False, ["20"], timeout=1
        )

    def test_keeps_selected_band_when_internet_returns(self):
        with (
            patch.object(lte_features, "read_band_config", return_value=self.previous),
            patch.object(lte_features, "apply_band_config") as apply,
            patch.object(lte_features, "wait_for_internet", return_value=True),
        ):
            result = self.call_helper()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(apply.call_args.args[-2:], (False, ["20"]))

    def test_restores_previous_band_when_target_has_no_internet(self):
        with (
            patch.object(lte_features, "read_band_config", return_value=self.previous),
            patch.object(lte_features, "apply_band_config") as apply,
            patch.object(
                lte_features, "wait_for_internet", side_effect=[False, True]
            ),
        ):
            result = self.call_helper()
        self.assertEqual(result["status"], "restored")
        self.assertEqual(apply.call_count, 2)
        self.assertEqual(apply.call_args_list[1].args[-2:], (False, ["3"]))

    def test_reports_when_previous_band_cannot_restore_internet(self):
        with (
            patch.object(lte_features, "read_band_config", return_value=self.previous),
            patch.object(lte_features, "apply_band_config"),
            patch.object(lte_features, "wait_for_internet", return_value=False),
        ):
            result = self.call_helper()
        self.assertEqual(result["status"], "restore_failed")
        self.assertIn("restore_error", result)


class LocalDataAndRedactionTests(unittest.TestCase):
    def test_diagnostic_redacts_password_token_and_sms_text(self):
        line = "password=secret token=abc Text: private SMS contents"
        result = app.redact_log_line(line)
        for secret in ("secret", "abc", "private SMS contents"):
            self.assertNotIn(secret, result)
        self.assertIn("[REDACTED]", result)
        self.assertIn("[SMS-Inhalt ausgelassen]", result)

    def test_old_sms_state_is_copied_to_per_user_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            old_state = base / "working" / "sms_state.json"
            new_state = base / "profile" / "sms_state.json"
            old_state.parent.mkdir()
            old_state.write_text('{"message-1": {"attempts": 1}}', encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                with (
                    patch.object(app, "APP_DATA_DIR", str(new_state.parent)),
                    patch.object(app, "SMS_STATE_FILE", str(new_state)),
                    patch.object(app, "LEGACY_SMS_STATE_FILE", str(old_state)),
                ):
                    state = app.lade_sms_state()
            self.assertEqual(state, {"message-1": {"attempts": 1}})
            self.assertTrue(new_state.exists())
            self.assertTrue(old_state.exists())

    def test_legacy_password_is_offered_for_dpapi_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            legacy = base / "cudy_credentials.json"
            legacy.write_text(json.dumps({"router_password": "test-password"}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                with (
                    patch.dict(os.environ, {"CUDY_ROUTER_PASSWORD": ""}),
                    patch.object(app, "CREDENTIALS_FILE", str(base / "router_password.dpapi")),
                    patch.object(app, "LEGACY_CREDENTIAL_FILES", (str(legacy),)),
                    patch.object(app, "speichere_router_passwort") as save,
                ):
                    password = app.lade_router_passwort()
            self.assertEqual(password, "test-password")
            if os.name == "nt":
                save.assert_called_once_with("test-password")

    def test_settings_are_saved_atomically_in_user_profile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "settings.json"
            values = {
                "router_url": "http://192.168.10.1",
                "poll_interval": 15,
                "session_refresh_interval": 600,
                "watchdog_timeout": 60,
            }
            with (
                patch.object(app, "APP_DATA_DIR", directory),
                patch.object(app, "SETTINGS_FILE", str(settings_file)),
            ):
                self.assertTrue(app.speichere_app_einstellungen(values))
                self.assertEqual(app.lade_app_einstellungen(), values)
            self.assertFalse(Path(str(settings_file) + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
