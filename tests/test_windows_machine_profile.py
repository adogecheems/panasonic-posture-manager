"""Windows product-family profiles and capability-based fallback."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from panasonic_posture.windows.service.application import create_hardware_runtime
from panasonic_posture.windows.hardware.machine_profile import (
    WINDOWS_PROFILE_DIR,
    WindowsProfile,
    resolve_windows_profile,
)

PROFILES = Path(__file__).resolve().parents[1] / 'data' / 'profiles'


class WindowsMachineProfileTests(unittest.TestCase):
    def test_default_profile_directory_is_packaged_beside_the_application(self):
        self.assertEqual(WINDOWS_PROFILE_DIR, PROFILES)

    def test_all_qv_families_have_explicit_windows_profiles(self):
        expected = (
            ('CFQV8', 'cfqv8'),
            ('CFQV8-1L', 'cfqv8'),
            ('CFQV8ANY', 'cfqv8'),
            ('CF-QV8', 'cfqv8'),
            ('CF-QV8-TEST', 'cfqv8'),
            ('CFQV9', 'cfqv9'),
            ('CFQV9-1L', 'cfqv9'),
            ('CFQV9ANY', 'cfqv9'),
            ('CF-QV9-TEST', 'cfqv9'),
            ('CFQV1', 'cfqv1'),
            ('CFQV1-1L', 'cfqv1'),
            ('CFQV1ANY', 'cfqv1'),
            ('CF-QV1-TEST', 'cfqv1'),
        )
        for product, identifier in expected:
            with self.subTest(product=product):
                profile = resolve_windows_profile(
                    ('Panasonic Corporation', product),
                    profile_dirs=(PROFILES,),
                )
                self.assertEqual(profile.id, identifier)
                self.assertEqual(profile.product, product)
                self.assertEqual(profile.support_tier, 'validated')

    def test_unknown_machine_uses_generic_capability_detection(self):
        profile = resolve_windows_profile(
            ('Another Vendor', 'Convertible-1'),
            profile_dirs=(PROFILES,),
        )
        self.assertEqual(profile.id, 'generic')
        self.assertEqual(profile.vendor, 'Another Vendor')
        self.assertEqual(profile.product, 'Convertible-1')
        self.assertEqual(profile.support_tier, 'compatible')

    def test_identity_whitespace_is_trimmed_before_profile_selection(self):
        profile = resolve_windows_profile(
            ('  Panasonic Corporation  ', ' CFQV9-1L '),
            profile_dirs=(PROFILES,),
        )
        self.assertEqual(profile.id, 'cfqv9')
        self.assertEqual(profile.product, 'CFQV9-1L')

    def test_unreadable_identity_still_uses_the_generic_capability_path(self):
        with patch(
            'panasonic_posture.windows.hardware.machine_profile.read_machine_identity',
            side_effect=OSError('BIOS identity unavailable'),
        ):
            profile = resolve_windows_profile(profile_dirs=(PROFILES,))
        self.assertEqual(profile.id, 'generic')
        self.assertEqual(profile.support_tier, 'compatible')

    def test_hardware_composition_uses_profile_capabilities_not_a_machine_gate(self):
        profile = WindowsProfile(
            id='generic',
            vendor='Another Vendor',
            product='Convertible-1',
            support_tier='compatible',
            mount_matrix=(1., 0., 0., 0., 1., 0., 0., 0., 1.),
        )
        with patch(
            'panasonic_posture.windows.service.application.WindowsSensor',
        ) as sensor_factory, patch(
            'panasonic_posture.windows.service.application.AcpiPhysicalSwitch',
        ) as switch_factory, patch(
            'panasonic_posture.windows.service.application.Engine',
        ) as engine_factory, patch(
            'panasonic_posture.windows.service.application.HardwareRuntime',
        ) as runtime_factory:
            create_hardware_runtime(runtime=Mock(), settings=Mock(), profile=profile)
        sensor_factory.assert_called_once_with(
            mount_matrix=profile.mount_matrix,
            include_orientation=False,
        )
        device = engine_factory.call_args.kwargs['device']
        self.assertEqual(device.id, 'generic')
        self.assertEqual(device.vendor, 'Another Vendor')
        self.assertEqual(device.product, 'Convertible-1')
        self.assertEqual(device.support_tier, 'compatible')
        self.assertFalse(
            switch_factory.call_args.kwargs['trusted_startup_baseline']
        )
        runtime_factory.assert_called_once()


if __name__ == '__main__':
    unittest.main()
