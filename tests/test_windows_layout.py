"""Windows source and installed bundle layout calculations."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.common.layout import distribution_root

ROOT = Path(__file__).resolve().parents[1]


class WindowsLayoutTests(unittest.TestCase):
    def test_source_modules_resolve_to_the_repository_root(self):
        modules = (
            ROOT / 'src/panasonic_posture/windows/app/resources.py',
            ROOT / 'src/panasonic_posture/windows/app/startup.py',
            ROOT / 'src/panasonic_posture/windows/common/layout.py',
            ROOT / 'src/panasonic_posture/windows/hardware/machine_profile.py',
            ROOT / 'src/panasonic_posture/windows/service/daemon.py',
            ROOT / 'src/panasonic_posture/windows/service/trusted_runtime.py',
        )
        for module in modules:
            with self.subTest(module=module.name):
                self.assertEqual(distribution_root(module), ROOT)

    def test_installed_module_resolves_above_the_app_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = (
                root
                / 'app/panasonic_posture/windows/hardware/machine_profile.py'
            )
            module.parent.mkdir(parents=True)
            module.touch()
            self.assertEqual(distribution_root(module), root.resolve())

    def test_unrelated_module_layout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / 'other/project/windows/domain/module.py'
            module.parent.mkdir(parents=True)
            module.touch()
            with self.assertRaisesRegex(ValueError, 'product layout'):
                distribution_root(module)


if __name__ == '__main__':
    unittest.main()
