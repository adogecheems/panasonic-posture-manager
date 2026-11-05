"""Safety contracts at the platform-neutral execution boundaries."""

import unittest


class PlatformSeparationTests(unittest.TestCase):
    def test_session_owner_is_namespaced(self):
        from panasonic_posture.service.ports import SessionOwner
        self.assertNotEqual(SessionOwner("linux", "1000"), SessionOwner("other", "1000"))
        self.assertEqual(SessionOwner("other", "sid/session"), SessionOwner("other", "sid/session"))
        with self.assertRaises(ValueError):
            SessionOwner("", "1000")

    def test_relay_completion_requires_both_effects(self):
        from panasonic_posture.service.actuator import RelayState
        from panasonic_posture.model import Target
        target = Target(mode="tent", tablet=True, disable_internal_input=True)
        for tablet in (False, True):
            for disabled in (False, True):
                state = RelayState(tablet, disabled, False, False)
                self.assertEqual(state.satisfies(target), tablet and disabled)
        self.assertFalse(RelayState(True, True, False, False, "failed").satisfies(target))

    def test_internal_sensor_status_preserves_non_linux_backend(self):
        from panasonic_posture.model import SensorInfo
        status = SensorInfo(ready=True, backend="test-sensor", device="panel-1")
        self.assertTrue(status.ready)
        self.assertEqual(status.backend, "test-sensor")
        self.assertEqual(status.device, "panel-1")

    def test_display_requires_explicit_storage(self):
        from panasonic_posture.session.transaction import SoftwareDisplay
        with self.assertRaises(TypeError):
            SoftwareDisplay()

    def test_transport_rejection_is_explicit(self):
        from panasonic_posture.session.failures import FailureKind, ServiceError
        from panasonic_posture.session.runtime import SessionRuntime
        self.assertTrue(SessionRuntime._is_lease_rejection(
            ServiceError(kind=FailureKind.REJECTED)))
        self.assertFalse(SessionRuntime._is_lease_rejection(
            ServiceError(detail="not-session-adapter", kind=FailureKind.TRANSIENT)))

    def test_linux_decoder_still_rejects_unknown_sensor_sources(self):
        from panasonic_posture.model import StateSnapshot, dumps_state, parse_state_snapshot
        with self.assertRaises(ValueError):
            parse_state_snapshot(dumps_state(StateSnapshot(sensor_source="test-sensor")))
        state = parse_state_snapshot(dumps_state(StateSnapshot(sensor_source="iio")))
        self.assertEqual(state.sensor_source, "iio")

    def test_linux_client_classifies_legacy_errors(self):
        from panasonic_posture.session.dbus_errors import service_error
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

    def test_known_denial_cannot_be_downgraded_to_transient(self):
        from panasonic_posture.contracts import ErrorCode
        from panasonic_posture.session.failures import FailureKind, ServiceError
        from panasonic_posture.session.runtime import SessionRuntime
        error = ServiceError(ErrorCode.ACCESS_DENIED, kind=FailureKind.TRANSIENT)
        self.assertTrue(SessionRuntime._is_lease_rejection(error))
