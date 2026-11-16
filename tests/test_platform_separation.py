"""Safety contracts at the platform-neutral execution boundaries."""

import unittest


class PlatformSeparationTests(unittest.TestCase):
    def test_session_owner_is_namespaced(self):
        from panasonic_posture.service.ports import SessionOwner
        self.assertNotEqual(SessionOwner("linux", "1000"), SessionOwner("other", "1000"))
        self.assertEqual(SessionOwner("other", "sid/session"), SessionOwner("other", "sid/session"))
        with self.assertRaises(ValueError):
            SessionOwner("", "1000")

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

    def test_known_denial_cannot_be_downgraded_to_transient(self):
        from panasonic_posture.contracts import ErrorCode
        from panasonic_posture.session.failures import FailureKind, ServiceError
        from panasonic_posture.session.runtime import SessionRuntime
        error = ServiceError(ErrorCode.ACCESS_DENIED, kind=FailureKind.TRANSIENT)
        self.assertTrue(SessionRuntime._is_lease_rejection(error))
