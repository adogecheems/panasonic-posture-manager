"""SCM lifecycle adapter for the installed LocalSystem hardware host."""
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from panasonic_posture.windows.common.layout import distribution_root
from panasonic_posture.windows.service import daemon
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
    def test_image_path_command_uses_the_install_root_after_the_service_move(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(daemon.main(['--image-path']), 0)
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(distribution_root(daemon.__file__), root)
        self.assertEqual(output.getvalue().strip(), service_image_path(root))

    def test_service_labels_use_the_application_display_name(self):
        self.assertEqual(
            SERVICE_DISPLAY_NAME, f'{APPLICATION_DISPLAY_NAME} Hardware Service'
        )
        self.assertIn(APPLICATION_DISPLAY_NAME, SERVICE_DESCRIPTION)

    def test_manual_service_install_localizes_simplified_chinese_labels(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/service/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertIn(
            "[Globalization.CultureInfo]::CurrentUICulture.Name -match "
            "'^zh-(CN|SG|Hans)(-|$)'",
            source,
        )
        self.assertIn('松下二合一平板电脑姿态管理工具 硬件服务', source)
        self.assertIn('提供经过身份验证的 LocalSystem 硬件端点', source)

    def test_manual_installer_checks_the_restructured_service_module(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/service/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertIn(
            r"'app\panasonic_posture\windows\service\daemon.py'",
            source,
        )
        self.assertNotIn(r"'app\panasonic_posture\windows\hardware_service.py'", source)

    def test_service_uses_non_delayed_automatic_start(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/service/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertEqual(
            source.count('sc.exe config $ServiceName start= auto obj= LocalSystem'),
            1,
        )
        self.assertNotIn('delayed-auto', source)

    @unittest.skipUnless(sys.platform == 'win32', 'PowerShell recovery fixture requires Windows')
    def test_recovery_covers_reported_failures_and_checks_each_configuration_step(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/service/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        helper = source[source.index('function Configure-ServiceRecovery'):source.index('function Protect-ServiceLogs')]
        configure = source[source.index("if ($Action -eq 'Configure')"):source.index("if ($Action -eq 'Remove')")]
        self.assertIn('Configure-ServiceRecovery', configure)
        # Execute the production helper with a command fake: never modify SCM.
        for failure in ('', 'config', 'failure', 'failureflag'):
            with self.subTest(failure=failure):
                script = (
                    "$ErrorActionPreference = 'Stop'\n"
                    "$ServiceName = 'FixtureHardware'\n"
                    "$script:commands = [Collections.Generic.List[string]]::new()\n"
                    "function sc.exe {\n"
                    "  $script:commands.Add(($args -join ' '))\n"
                    f"  $global:LASTEXITCODE = if ($args[0] -eq '{failure}') {{ 5 }} else {{ 0 }}\n"
                    "}\n" + helper +
                    "try { Configure-ServiceRecovery; $success = $true } "
                    "catch { $success = $false }\n"
                    "@{success=$success; commands=@($script:commands.ToArray())} | ConvertTo-Json -Compress\n"
                )
                result = subprocess.run(
                    ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['success'], not failure)
                expected = [
                    'config FixtureHardware start= auto obj= LocalSystem',
                    'failure FixtureHardware reset= 86400 actions= restart/5000/restart/15000/restart/60000',
                    'failureflag FixtureHardware 1',
                ]
                count = {'': 3, 'config': 1, 'failure': 2, 'failureflag': 3}[failure]
                self.assertEqual(payload['commands'], expected[:count])

    def test_service_start_is_bounded_so_a_failure_cannot_hang_install(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'packaging/windows/service/hardware-service.ps1'
        ).read_text(encoding='utf-8')
        self.assertIn('function Start-InstalledService', source)
        self.assertIn('$service.Start()', source)
        self.assertIn("Wait-ServiceState 'Running' 30", source)
        self.assertNotIn('Start-Service -Name $ServiceName', source)
        configure = source[
            source.index("if ($Action -eq 'Configure')"):
            source.index("if ($Action -eq 'Remove')")
        ]
        self.assertIn('Start-InstalledService', configure)

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
        self.assertEqual(reports[-1][1]['service_specific_exit_code'], 0)

    def test_construction_and_cleanup_failures_report_nonzero_status_for_recovery(self):
        for stage in ('construction', 'cleanup'):
            with self.subTest(stage=stage):
                reports, events = [], []
                stop = threading.Event()
                stop.set()
                runtime = Runtime(events)

                def fail():
                    raise OSError(f'fixture {stage} failure')

                if stage == 'cleanup':
                    runtime.close = fail
                factory = fail if stage == 'construction' else lambda: runtime
                runner = HardwareServiceRunner(factory, tick=.001)
                with self.assertLogs(daemon.log, level='ERROR'):
                    code = runner.run(stop, lambda state, **values: reports.append((state, values)))
                self.assertEqual(code, ERROR_SERVICE_SPECIFIC_ERROR)
                self.assertEqual(reports[-1][0], SERVICE_STOPPED)
                self.assertEqual(reports[-1][1]['win32_exit_code'], code)
                self.assertEqual(reports[-1][1]['service_specific_exit_code'], 1)

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
        root = Path(__file__).resolve().parent / 'PanasonicPostureManager'
        command = service_image_path(root)
        self.assertEqual(
            command,
            f'"{root.absolute() / "runtime/python-hardware.exe"}" '
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
