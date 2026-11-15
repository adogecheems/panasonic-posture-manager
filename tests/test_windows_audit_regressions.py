"""Production wiring and lifecycle regressions from the Windows audit."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from panasonic_posture.model import HardwareStatus, SensorInfo, SensorReading
from panasonic_posture.windows.service.application import create_hardware_runtime

ROOT = Path(__file__).resolve().parents[1]


class ProductionSettingsTests(unittest.TestCase):
    def test_default_store_survives_a_new_production_engine(self):
        now = [1000.]
        sensor = SimpleNamespace(info=SensorInfo(ready=True, backend='windows-winrt'),
                                 reading=lambda: SensorReading((2, 0, -9.8), now[0]))
        profile = SimpleNamespace(mount_matrix=(1, 0, 0, 0, 1, 0, 0, 0, 1),
                                  support_tier='validated', id='test', vendor='test', product='test')
        with TemporaryDirectory() as directory, patch.dict(os.environ, ProgramData=directory), \
                patch('panasonic_posture.windows.service.application.WindowsSensor', return_value=sensor), \
                patch('panasonic_posture.windows.service.application.AcpiPhysicalSwitch',
                      return_value=SimpleNamespace(query=lambda: (False, HardwareStatus.OK))):
            first = create_hardware_runtime(runtime=Mock(), profile=profile, wake=Mock(), clock=lambda: now[0])
            self.addCleanup(first.engine.shutdown)
            first.engine.set_tent_enabled(False)
            first.engine.set_tent_prompt(False)
            first.engine.set_stand_prompt(True)
            for _ in range(3):
                first.engine.tick()
                now[0] += .1
            first.engine.calibrate_sensor()
            second = create_hardware_runtime(runtime=Mock(), profile=profile, wake=Mock())
            self.addCleanup(second.engine.shutdown)
            self.assertFalse(second.engine.state.tent_enabled)
            self.assertFalse(second.engine.state.tent_prompt)
            self.assertTrue(second.engine.state.stand_prompt)
            self.assertTrue(second.engine.state.sensor_calibrated)
            self.assertTrue((Path(directory) / 'PanasonicPostureManager/State/settings.json').is_file())


class InstallerRollbackTests(unittest.TestCase):
    def test_existing_configuration_is_not_removed_by_upgrade_or_repair_rollback(self):
        root = ET.parse(ROOT / 'packaging/windows/msi/Package.wxs').getroot()
        action = next(e for e in root.iter() if e.tag.endswith('Custom') and e.get('Action') == 'RollbackSlate')
        self.assertEqual(action.get('Condition'), 'NOT REMOVE AND SLATE_CONFIGURED_BY_PRODUCT <> "#1"')
