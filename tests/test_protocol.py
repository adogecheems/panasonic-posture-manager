"""Tests for wire protocol constants and error helpers (Lead integration scope)."""

import unittest

from panasonic_posture import protocol


class ErrorCodeTests(unittest.TestCase):
    def test_round_trip_with_detail(self):
        message = protocol.error_message(protocol.ErrorCode.DISPLAY_LAYOUT, "external screen")
        self.assertEqual(message, "display-layout-conflict: external screen")
        self.assertEqual(
            protocol.split_error(message),
            (protocol.ErrorCode.DISPLAY_LAYOUT, "external screen"),
        )

    def test_round_trip_without_detail(self):
        self.assertEqual(
            protocol.split_error(protocol.error_message(protocol.ErrorCode.INVALID_MODE)),
            (protocol.ErrorCode.INVALID_MODE, None),
        )

    def test_unknown_message_becomes_internal(self):
        code, detail = protocol.split_error("some old backend text")
        self.assertEqual(code, protocol.ErrorCode.INTERNAL)
        self.assertEqual(detail, "some old backend text")

    def test_every_code_is_registered(self):
        for name in dir(protocol.ErrorCode):
            value = getattr(protocol.ErrorCode, name)
            if not name.startswith("_") and isinstance(value, str):
                self.assertIn(value, protocol.ALL_ERROR_CODES)


class ContractTests(unittest.TestCase):
    def test_identity_constants(self):
        self.assertEqual(protocol.NAME, "work.mmoe.panasonic.Posture")
        self.assertEqual(protocol.PATH, "/work/mmoe/panasonic/Posture")
        self.assertEqual(protocol.SESSION_NAME_PREFIX, protocol.NAME + ".Session.")
        self.assertEqual(protocol.session_name(1000),
                         "work.mmoe.panasonic.Posture.Session.u1000")
        self.assertTrue(protocol.session_name(1000).startswith(
            protocol.SESSION_NAME_PREFIX))
        self.assertEqual(protocol.POLKIT_ACTION, protocol.NAME + ".configure")
        self.assertEqual(protocol.STATE_VERSION, 2)
        self.assertEqual(protocol.RELAY_PROTOCOL, 3)

    def test_dbus_xml_has_retry(self):
        self.assertIn('<method name="Retry"/>', protocol.DBUS_XML)

    def test_dbus_xml_exposes_stand_prompt_methods(self):
        self.assertIn('<method name="SetStandPrompt">', protocol.DBUS_XML)
        self.assertIn('<method name="ConfirmStand">', protocol.DBUS_XML)

    def test_dbus_xml_exposes_tent_mode_switch(self):
        self.assertIn('<method name="SetTentEnabled">', protocol.DBUS_XML)

    def test_dbus_xml_exposes_sensor_calibration_methods(self):
        self.assertIn('<method name="CalibrateSensor"/>', protocol.DBUS_XML)
        self.assertIn('<method name="ResetSensorCalibration"/>', protocol.DBUS_XML)

    def test_access_denied_error_code(self):
        self.assertIn(protocol.ErrorCode.ACCESS_DENIED, protocol.ALL_ERROR_CODES)

    def test_dbus_xml_has_revised_session_update(self):
        xml = protocol.DBUS_XML
        self.assertIn('name="SessionUpdate"', xml)
        section = xml.split('name="SessionUpdate"', 1)[1].split("</method>", 1)[0]
        self.assertEqual(section.count('direction="in"'), 4)
        for arg in ("u", "b", "s", "s"):
            self.assertIn(f'type="{arg}" direction="in"', section)

    def test_enum_validation_sets(self):
        self.assertEqual(set(protocol.MODES), {"auto", "laptop", "tent", "stand", "tablet"})
        self.assertIn("hardware", protocol.TARGET_MODES)
        self.assertIn("unavailable", protocol.TARGET_MODES)
        self.assertIn("absent", protocol.HARDWARE_STATUSES)
        self.assertEqual(set(protocol.PROMPTS), {"tent", "stand"})


if __name__ == "__main__":
    unittest.main()
