"""Default-on login startup, command quoting and GUI activation without host writes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from panasonic_posture.windows.activation import WindowActivation
from panasonic_posture.windows.startup import (
    initialize_default_startup, LoginStartup, StartupError, UnavailableStartup, startup_command,
)


class Registry:
    def __init__(self):
        self.value = None
        self.fail = False
        self.ignore = False
        self.writes = []
        self.initialized = False
        self.marker_writes = 0
        self.ignore_marker = False
    def read(self): return self.value
    def write(self, command):
        self.writes.append(command)
        if self.fail: raise PermissionError('denied')
        if not self.ignore: self.value = command
    def delete(self):
        if self.fail: raise PermissionError('denied')
        self.value = None
    def read_initialized(self): return self.initialized
    def mark_initialized(self):
        self.marker_writes += 1
        if self.fail: raise PermissionError('denied')
        if not self.ignore_marker: self.initialized = True


class LoginStartupTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.command = [str(Path(sys.executable).resolve()), 'launch file.py', '--background']
        self.startup = LoginStartup(self.command, registry=self.registry)

    def test_first_launch_default_write_readback_disable_and_explicit_choice(self):
        self.assertFalse(self.startup.status().enabled)
        self.assertTrue(self.startup.status().available)
        self.startup.ensure_default_enabled()
        self.assertTrue(self.startup.status().enabled)
        self.assertTrue(self.registry.initialized)
        self.assertEqual(len(self.registry.writes), 1)
        self.startup.ensure_default_enabled()
        self.assertEqual(len(self.registry.writes), 1)
        self.startup.set_enabled(False)
        self.assertFalse(self.startup.status().enabled)
        self.assertIsNone(self.registry.value)
        LoginStartup(self.command, registry=self.registry).ensure_default_enabled()
        self.assertIsNone(self.registry.value)
        self.assertEqual(self.registry.writes, [subprocess.list2cmdline(self.command)])

    def test_enable_write_readback_and_idempotence(self):
        self.startup.set_enabled(True)
        self.assertTrue(self.startup.status().enabled)
        self.assertEqual(self.registry.value, subprocess.list2cmdline(self.command))
        self.startup.set_enabled(True)
        self.assertEqual(len(self.registry.writes), 1)
        self.startup.set_enabled(False)
        self.assertFalse(self.startup.status().enabled)
        self.assertIsNone(self.registry.value)

    def test_upgrade_migrates_a_recognized_previous_command_in_place(self):
        command = [self.command[0], '-B', *self.command[1:]]
        startup = LoginStartup(command, registry=self.registry)
        self.registry.value = subprocess.list2cmdline(self.command)
        self.registry.initialized = True

        status = startup.status()
        self.assertTrue(status.enabled)
        self.assertTrue(status.available)
        self.assertFalse(status.conflict)

        startup.ensure_default_enabled()
        self.assertEqual(self.registry.value, startup.command)
        self.assertEqual(self.registry.writes, [startup.command])
        self.assertEqual(self.registry.marker_writes, 0)

    def test_previous_command_can_be_disabled_without_reopening_it(self):
        command = [self.command[0], '-B', *self.command[1:]]
        startup = LoginStartup(command, registry=self.registry)
        self.registry.value = subprocess.list2cmdline(self.command)
        self.registry.initialized = True

        startup.set_enabled(False)
        self.assertIsNone(self.registry.value)
        startup.ensure_default_enabled()
        self.assertIsNone(self.registry.value)
        self.assertFalse(startup.status().enabled)
        self.assertTrue(startup.status().available)

    def test_previous_command_migration_requires_readback(self):
        command = [self.command[0], '-B', *self.command[1:]]
        startup = LoginStartup(command, registry=self.registry)
        legacy = subprocess.list2cmdline(self.command)
        self.registry.value = legacy
        self.registry.initialized = True
        self.registry.ignore = True

        with self.assertRaises(StartupError):
            startup.ensure_default_enabled()
        self.assertEqual(self.registry.value, legacy)

    def test_foreign_or_replaced_entry_is_preserved_and_never_overwritten_or_deleted(self):
        for enabled in (True, False):
            self.registry.value = 'another user command'
            self.assertTrue(self.startup.status().conflict)
            self.assertFalse(self.startup.status().available)
            with self.assertRaises(StartupError): self.startup.set_enabled(enabled)
            with self.assertRaises(StartupError): self.startup.ensure_default_enabled()
            self.assertEqual(self.registry.value, 'another user command')
        self.assertEqual(self.registry.writes, [])
        self.assertEqual(self.registry.marker_writes, 0)
        self.registry.initialized = True
        with self.assertRaises(StartupError):
            self.startup.ensure_default_enabled()
        self.assertEqual(self.registry.value, 'another user command')
        self.assertEqual(self.registry.writes, [])

    def test_denied_and_ignored_writes_do_not_report_enabled(self):
        self.registry.fail = True
        with self.assertRaises(PermissionError): self.startup.ensure_default_enabled()
        self.assertFalse(self.startup.status().enabled)
        self.registry.fail = False
        self.registry.ignore = True
        with self.assertRaises(StartupError): self.startup.set_enabled(True)
        self.assertFalse(self.startup.status().enabled)
        self.registry.ignore = False
        self.registry.initialized = False
        self.registry.ignore_marker = True
        writes = len(self.registry.writes)
        with self.assertRaises(StartupError): self.startup.set_enabled(True)
        self.assertEqual(len(self.registry.writes), writes)

    def test_invalid_and_overlong_commands_fail_before_registry_writes(self):
        for command in ([], ['relative.exe'], [self.command[0], 'bad\0argument'],
                        [self.command[0], '😀' * 150]):
            with self.subTest(command=command), self.assertRaises((ValueError, StartupError)):
                LoginStartup(command, registry=self.registry)
        for enabled in ('yes', 1, None):
            with self.assertRaises(ValueError): self.startup.set_enabled(enabled)
        self.assertEqual(self.registry.writes, [])

    def test_source_and_bundle_commands_are_absolute_fixed_and_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / 'pythonw.exe').touch()
            (root / 'launch.py').touch()
            self.assertEqual(startup_command(executable=runtime / 'python.exe', root=root),
                (str(runtime / 'pythonw.exe'), '-B', '-I', str(root / 'launch.py'), '--background'))
            main = root / 'selected main'
            main.mkdir()
            (main / 'launch.py').touch()
            service_runtime = root / 'service/runtime'
            service_runtime.mkdir(parents=True)
            (service_runtime / 'pythonw.exe').touch()
            self.assertEqual(
                startup_command(
                    executable=service_runtime / 'python.exe',
                    root=main,
                ),
                (
                    str(service_runtime / 'pythonw.exe'),
                    '-B',
                    '-I',
                    str(main / 'launch.py'),
                    '--background',
                ),
            )
            (root / 'launch.py').unlink()
            with self.assertRaises(StartupError): startup_command(executable=runtime / 'python.exe', root=root)
            (root / 'src/panasonic_posture').mkdir(parents=True)
            entry = root / 'packaging/windows/launch-hardware.py'
            entry.parent.mkdir(parents=True)
            entry.touch()
            self.assertEqual(startup_command(executable=runtime / 'python.exe', root=root),
                             (str(runtime / 'pythonw.exe'), str(entry)))
            with self.assertRaises(StartupError): startup_command(executable=Path('relative.exe'), root=root)

    def test_unavailable_startup_does_not_register_anything(self):
        startup = UnavailableStartup()
        startup.ensure_default_enabled()
        self.assertFalse(startup.status().enabled)
        self.assertFalse(startup.status().available)

    def test_default_initialization_failure_is_diagnostic_but_not_fatal(self):
        class Failing:
            def ensure_default_enabled(self):
                raise StartupError('startup-entry-conflict')

        with self.assertLogs('panasonic_posture.windows.startup', level='WARNING') as logs:
            initialize_default_startup(Failing())
        self.assertIn('startup-entry-conflict', logs.output[0])


class WindowActivationTests(unittest.TestCase):
    def test_relaunch_requests_show_but_background_relaunch_does_not_and_stale_hint_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = WindowActivation(root), WindowActivation(root)
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire(background=True))
                self.assertFalse(first.poll())
                self.assertFalse(second.acquire())
                self.assertTrue(first.poll())
                self.assertFalse(first.poll())
                self.assertEqual(first.request.stat().st_size, 0)
            finally:
                first.close()
            self.assertTrue(second.acquire())
            self.assertFalse(second.poll())
            second.close()
            second.close()

    def test_path_or_permission_failure_is_not_mistaken_for_an_existing_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            activation = WindowActivation(Path(directory))
            with patch('panasonic_posture.windows.instance.os.open', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError): activation.acquire()
            self.assertFalse(activation.request.exists())


if __name__ == '__main__': unittest.main()
