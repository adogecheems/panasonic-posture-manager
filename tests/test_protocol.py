"""Tests for wire protocol constants and error helpers (Lead integration scope)."""

import unittest
import xml.etree.ElementTree as ET

from panasonic_posture.linux import protocol


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
    def dbus_method(self, name):
        root = ET.fromstring(protocol.DBUS_XML)
        interface = root.find("interface")
        self.assertIsNotNone(interface)
        self.assertEqual(interface.get("name"), protocol.IFACE)
        method = next(
            (
                element
                for element in interface
                if element.tag == "method" and element.get("name") == name
            ),
            None,
        )
        self.assertIsNotNone(method, name)
        return method

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
        self.assertEqual(protocol.RELAY_PROTOCOL, 4)

    def test_dbus_xml_has_contact_backed_session_wake(self):
        arguments = self.dbus_method("SessionWake").findall("arg")
        self.assertEqual(
            [(argument.get("type"), argument.get("direction")) for argument in arguments],
            [("u", "in"), ("t", "in")],
        )

    def test_dbus_xml_has_retry(self):
        self.assertEqual(len(self.dbus_method("Retry")), 0)

    def test_dbus_xml_exposes_stand_prompt_methods(self):
        root = ET.fromstring(protocol.DBUS_XML)
        methods = {
            element.get("name")
            for element in root.find("interface")
            if element.tag == "method"
        }
        self.assertNotIn("SetSuspendTouchWake", methods)
        self.assertIn("SetStandPrompt", methods)
        self.assertIn("ConfirmStand", methods)

    def test_dbus_xml_exposes_tent_mode_switch(self):
        self.assertIsNotNone(self.dbus_method("SetTentEnabled"))

    def test_dbus_xml_exposes_sensor_calibration_methods(self):
        self.assertEqual(len(self.dbus_method("CalibrateSensor")), 0)
        self.assertEqual(len(self.dbus_method("ResetSensorCalibration")), 0)

    def test_access_denied_error_code(self):
        self.assertIn(protocol.ErrorCode.ACCESS_DENIED, protocol.ALL_ERROR_CODES)

    def test_dbus_xml_has_revised_session_update(self):
        arguments = self.dbus_method("SessionUpdate").findall("arg")
        self.assertEqual(
            [(argument.get("type"), argument.get("direction")) for argument in arguments],
            [("u", "in"), ("b", "in"), ("s", "in"), ("s", "in")],
        )

    def test_enum_validation_sets(self):
        self.assertEqual(set(protocol.MODES), {"auto", "laptop", "tent", "stand"})
        self.assertIn("hardware", protocol.TARGET_MODES)
        self.assertIn("unavailable", protocol.TARGET_MODES)
        self.assertIn("absent", protocol.HARDWARE_STATUSES)
        self.assertEqual(set(protocol.PROMPTS), {"tent", "stand"})


if __name__ == "__main__":
    unittest.main()
