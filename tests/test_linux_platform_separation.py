"""Linux adapter contracts kept outside the shared safety suite."""
import unittest


class LinuxPlatformSeparationTests(unittest.TestCase):
    def test_relay_completion_requires_both_effects(self):
        from panasonic_posture.linux.service.actuator import RelayState
        from panasonic_posture.model import Target
        target = Target(mode="tent", tablet=True, disable_internal_input=True)
        for tablet in (False, True):
            for disabled in (False, True):
                state = RelayState(tablet, disabled, False, False)
                self.assertEqual(state.satisfies(target), tablet and disabled)
        self.assertFalse(RelayState(True, True, False, False, "failed").satisfies(target))

    def test_linux_decoder_still_rejects_unknown_sensor_sources(self):
        from panasonic_posture.model import StateSnapshot, dumps_state
        from panasonic_posture.linux.contracts.state_codec import parse_state_snapshot
        with self.assertRaises(ValueError):
            parse_state_snapshot(dumps_state(StateSnapshot(sensor_source="test-sensor")))
        state = parse_state_snapshot(dumps_state(StateSnapshot(sensor_source="iio")))
        self.assertEqual(state.sensor_source, "iio")

    def test_linux_client_classifies_legacy_errors(self):
        from panasonic_posture.linux.session.dbus_errors import service_error
        from panasonic_posture.session.failures import FailureKind
        from panasonic_posture.contracts import ErrorCode
        for name in ("AccessDenied", "AuthFailed", "NotAuthorized"):
            self.assertEqual(service_error(ErrorCode.INTERNAL,
                name="org.freedesktop.DBus.Error." + name).kind, FailureKind.REJECTED)
        for detail in ("access-denied", "not-session-adapter", "session-lease-busy"):
            self.assertEqual(service_error(ErrorCode.INTERNAL, detail).kind, FailureKind.REJECTED)
        self.assertEqual(service_error(ErrorCode.INTERNAL,
            name="org.freedesktop.DBus.Error.NoReply").kind, FailureKind.TRANSIENT)
        self.assertEqual(service_error(ErrorCode.INTERNAL,
            name="org.freedesktop.DBus.Error.ServiceUnknown").kind, FailureKind.UNAVAILABLE)
