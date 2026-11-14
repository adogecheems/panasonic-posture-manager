"""SCM lifecycle adapter for the installed LocalSystem hardware host."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from panasonic_posture.windows.service.daemon import (
    ERROR_SERVICE_SPECIFIC_ERROR,
    HardwareServiceRunner,
    SERVICE_DESCRIPTION,
    SERVICE_DISPLAY_NAME,
    SERVICE_RUNNING,
    SERVICE_START_PENDING,
    SERVICE_STOPPED,
    SERVICE_STOP_PENDING,
    service_image_path,
)
from panasonic_posture.product import APPLICATION_DISPLAY_NAME


class Runtime:
    def __init__(self, events, stop=None):
        self.events = events
        self.stop = stop
        self.polls = 0

    def start(self):
        self.events.append('start')

    def poll(self):
        self.polls += 1
        self.events.append('poll')
        if self.stop is not None and self.polls == 2:
            self.stop.set()

    def close(self):
        self.events.append('close')


class HardwareServiceRunnerTests(unittest.TestCase):
    def test_service_labels_use_the_application_display_name(self):
        self.assertEqual(
            SERVICE_DISPLAY_NAME, f'{APPLICATION_DISPLAY_NAME} Hardware Service'
        )
        self.assertIn(APPLICATION_DISPLAY_NAME, SERVICE_DESCRIPTION)

    def test_manual_service_install_localizes_simplified_chinese_labels(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertIn(
            "[Globalization.CultureInfo]::CurrentUICulture.Name -match "
            "'^zh-(CN|SG|Hans)(-|$)'",
            source,
        )
        self.assertIn('松下二合一平板电脑姿态管理工具 硬件服务', source)
        self.assertIn('提供经过身份验证的 LocalSystem 硬件端点', source)

    def test_service_uses_non_delayed_automatic_start(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertEqual(
            source.count('sc.exe config $ServiceName start= auto obj= LocalSystem'),
            2,
        )
        self.assertNotIn('delayed-auto', source)

    def test_service_reports_startup_polling_and_clean_stop(self):
        events, reports = [], []
        stop = threading.Event()
        runtime = Runtime(events, stop)
        runner = HardwareServiceRunner(lambda: runtime, tick=.001)
        self.assertEqual(runner.run(stop, lambda state, **values: reports.append((state, values))), 0)
        self.assertEqual(events, ['start', 'poll', 'poll', 'close'])
        self.assertEqual([state for state, _ in reports], [
            SERVICE_START_PENDING, SERVICE_START_PENDING, SERVICE_RUNNING,
            SERVICE_STOP_PENDING, SERVICE_STOPPED,
        ])
        self.assertEqual(reports[-1][1]['win32_exit_code'], 0)

    def test_startup_failure_is_reported_and_owned_resources_are_closed(self):
        events, reports = [], []
        runtime = Runtime(events)

        def fail():
            events.append('start-failed')
            raise OSError('fixture setup failure')

        runtime.start = fail
        runner = HardwareServiceRunner(lambda: runtime, tick=.001)
        self.assertEqual(runner.run(threading.Event(), lambda state, **values: reports.append((state, values))),
                         ERROR_SERVICE_SPECIFIC_ERROR)
        self.assertEqual(events, ['start-failed', 'close'])
        self.assertNotIn(SERVICE_RUNNING, [state for state, _ in reports])
        self.assertEqual(reports[-1][0], SERVICE_STOPPED)
        self.assertEqual(reports[-1][1]['win32_exit_code'], ERROR_SERVICE_SPECIFIC_ERROR)
        self.assertEqual(reports[-1][1]['service_specific_exit_code'], 1)

    def test_poll_failure_still_reports_stopped_after_cleanup(self):
        events, reports = [], []
        runtime = Runtime(events)

        def fail():
            raise OSError('fixture polling failure')

        runtime.poll = fail
        runner = HardwareServiceRunner(lambda: runtime, tick=.001)
        self.assertEqual(runner.run(threading.Event(), lambda state, **values: reports.append((state, values))),
                         ERROR_SERVICE_SPECIFIC_ERROR)
        self.assertEqual(events, ['start', 'close'])
        self.assertEqual(reports[-1][0], SERVICE_STOPPED)

    def test_scm_command_uses_the_restricted_interpreter_and_fixed_module(self):
        command = service_image_path(Path(r'C:\Program Files\PanasonicPostureManager'))
        self.assertEqual(
            command,
            r'"C:\Program Files\PanasonicPostureManager\runtime\python-hardware.exe" '
            '-B -I -m panasonic_posture.windows.service.daemon --serve',
        )

    @unittest.skipUnless(sys.platform == 'win32', 'SCM ABI requires Windows')
    def test_serve_dispatch_reaches_scm_without_a_native_argument_error(self):
        root = Path(__file__).resolve().parents[1]
        source = json.dumps(str(root / 'src'))
        code = (
            'import runpy, sys; '
            f'sys.path.insert(0, {source}); '
            "sys.argv = ['hardware_service', '--serve']; "
            "runpy.run_module('panasonic_posture.windows.service.daemon', run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, '-B', '-c', code],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1063, result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
