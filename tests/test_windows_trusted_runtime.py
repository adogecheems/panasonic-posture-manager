"""Privileged launch files must be immutable to ordinary users and isolated."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.trusted_runtime import (
    Ace, protected_acl, TrustedRuntime, SYSTEM, ADMINS,
)


class ACLTests(unittest.TestCase):
    def test_only_trusted_owners_and_effective_write_grants_are_allowed(self):
        read = Ace('S-1-5-32-545', 0x1200a9)
        for owner in (SYSTEM, ADMINS):
            self.assertTrue(protected_acl(owner, [read, Ace(SYSTEM, 0x1f01ff)]))
        self.assertFalse(protected_acl('S-1-5-21-1000', [read]))
        for mask in (2, 4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000):
            with self.subTest(mask=mask):
                self.assertFalse(protected_acl(SYSTEM, [read, Ace('S-1-1-0', mask)]))
        self.assertTrue(protected_acl(SYSTEM, [read, Ace('S-1-1-0', 0x1f01ff, flags=8)]))
        self.assertTrue(protected_acl(SYSTEM, [read, Ace('S-1-1-0', 0x1f01ff, kind=1)]))
        self.assertFalse(protected_acl(SYSTEM, [read, Ace('S-1-1-0', 0, kind=5)]))
        self.assertFalse(protected_acl(SYSTEM, None))

    def test_drive_ancestor_may_allow_creating_subdirectories_but_not_replacement(self):
        self.assertTrue(protected_acl(ADMINS, [Ace('S-1-5-11', 4)], ancestor=True))
        for mask in (2, 0x40, 0x10000, 0x40000, 0x80000):
            self.assertFalse(protected_acl(ADMINS, [Ace('S-1-5-11', mask)], ancestor=True))


class Guard:
    def __init__(self):
        self.paths = []
        self.closed = False
    def pin(self, path, *, ancestor=False):
        self.paths.append((path, ancestor))
    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ('runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python-hardware.exe',
                     'runtime/Lib/os.py',
                     'runtime/DLLs/dummy.dll',
                     'app/panasonic_posture/windows/hardware_service.py',
                     'app/panasonic_posture/windows/session_agent.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        (self.root / 'runtime/python._pth').write_text(
            'Lib\nDLLs\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
        (self.root / 'runtime/pythonw._pth').write_text(
            'Lib\nDLLs\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
        (self.root / 'runtime/python-hardware._pth').write_text(
            'Lib\nDLLs\n../app\n', encoding='utf-8')
        self.guard = Guard()

    def runtime(self):
        runtime = TrustedRuntime(self.root, verifier=self.guard)
        self.addCleanup(runtime.close)
        return runtime

    def test_fixed_program_and_isolated_arguments_are_derived_from_pinned_installation(self):
        runtime = self.runtime()
        journal = self.root.parent / 'user data' / 'display.json'
        self.assertEqual(runtime.agent_arguments(journal), [str(self.root / 'runtime/python-hardware.exe'),
            '-B', '-I', '-m', 'panasonic_posture.windows.session_agent', '--journal', str(journal)])
        paths = [path for path, _ in self.guard.paths]
        self.assertIn(self.root / 'runtime/python-hardware.exe', paths)
        self.assertIn(self.root / 'app/panasonic_posture/windows/session_agent.py', paths)
        stop = 'Global\\PanasonicPostureManager.Stop.' + 'a' * 32
        self.assertEqual(runtime.agent_arguments(journal, stop_event=stop)[-2:], ['--stop-event', stop])
        with self.assertRaises(ValueError):
            runtime.agent_arguments(journal, stop_event='arbitrary-kernel-object')
        runtime.close()
        self.assertTrue(self.guard.closed)
        with self.assertRaises(OSError):
            runtime.agent_arguments(journal)

    def test_site_import_or_an_alternate_search_path_cannot_enter_privileged_runtime(self):
        for text in ('Lib\nDLLs\n../app\nimport site\n', 'Lib\nDLLs\n../app\nC:/user-code\n'):
            (self.root / 'runtime/python-hardware._pth').write_text(text, encoding='utf-8')
            guard = Guard()
            with self.assertRaises(ValueError):
                TrustedRuntime(self.root, verifier=guard)
            self.assertTrue(guard.closed)
        (self.root / 'runtime/python-hardware._pth').write_text(
            'Lib\nDLLs\n../app\n', encoding='utf-8')
        (self.root / 'runtime/python314._pth').write_text(
            'Lib\nDLLs\n../app\n', encoding='utf-8')
        guard = Guard()
        with self.assertRaises(ValueError):
            TrustedRuntime(self.root, verifier=guard)
        self.assertTrue(guard.closed)

    def test_missing_program_and_guard_failure_do_not_leave_pins_open(self):
        (self.root / 'runtime/python-hardware.exe').unlink()
        with self.assertRaises(ValueError):
            TrustedRuntime(self.root, verifier=self.guard)
        self.assertTrue(self.guard.closed)
        guard = Guard()
        def fail(path, **kwargs):
            raise PermissionError('fixture writable path')
        guard.pin = fail
        with self.assertRaises(PermissionError):
            TrustedRuntime(self.root, verifier=guard)
        self.assertTrue(guard.closed)
