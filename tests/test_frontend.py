"""Framework-free UI rules, including stale callbacks and failed writes."""
from concurrent.futures import Future
import subprocess
import sys
import unittest

from panasonic_posture.frontend.state import PostureModel, Detail
from panasonic_posture.frontend.actions import SettingsModel, ActionRunner
from panasonic_posture.model import StateSnapshot


def state(**values):
    result = StateSnapshot().to_dict()
    result.update(instance='one', generation=1, target={'mode': 'tent'},
                  manual='tent', pose='laptop', hardware=False,
                  session_connected=True, supported=True, support_tier='validated',
                  sensor_source='test-sensor')
    result.update(values)
    return result


class Client:
    def __init__(self):
        self.calls = []
        self.refreshes = 0

    def set_mode(self, value):
        future = Future()
        self.calls.append(('mode', value, future))
        return future

    def set_preference(self, field, value):
        future = Future()
        self.calls.append((field, value, future))
        return future

    def refresh(self):
        self.refreshes += 1


class FrontendTests(unittest.TestCase):
    def test_import_without_gui_or_linux(self):
        result = subprocess.run([sys.executable, '-S', '-c', '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == 'gi' or name.startswith(('PyQt', 'PySide')) or name.endswith('linux_contract'):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
from panasonic_posture.frontend import ports, state, actions
'''], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_transition_evidence_and_invalidation(self):
        model = PostureModel()
        model.update(state(), 10)
        self.assertEqual(model.pose, 'tent')
        self.assertEqual(model.detail, Detail.NONE)
        model.update(state(), 11)
        self.assertEqual(model.deadline, 12)
        model.advance(12)
        self.assertEqual(model.detail, Detail.SLOW)
        model.update(state(instance='two'), 13)
        self.assertFalse(model.slow)
        model.update(state(instance='two', applied={'mode': 'tent'}), 14)
        self.assertIsNone(model.deadline)
        model.update(state(error_code='internal-error'), 15)
        self.assertEqual(model.pose, 'laptop')
        self.assertEqual(model.detail, Detail.ERROR)
        model.disconnect()
        self.assertIsNone(model.deadline)
        self.assertEqual(model.detail, Detail.DISCONNECTED)

    def test_return_auto_requires_supported_connected_manual_state(self):
        model = PostureModel()
        self.assertFalse(model.can_return_auto)
        for values, expected in (({'manual': 'tent'}, True),
                                 ({'manual': 'auto'}, False),
                                 ({'hardware': True}, False),
                                 ({'supported': False}, False)):
            model.update(state(**values), 0)
            self.assertEqual(model.can_return_auto, expected)
        model.disconnect()
        self.assertFalse(model.can_return_auto)
        settings = SettingsModel(Client(), lambda: None, lambda error: None)
        settings.update(state(tent_enabled=False, hardware=False))
        self.assertTrue(settings.mode_enabled('tent'))

    def test_display_and_detail_priority(self):
        model = PostureModel()
        model.update(state(applied={'mode': 'hardware'}), 0)
        self.assertEqual(model.pose, 'tablet')
        model.update(state(pose='unknown', manual='auto', target={'mode': 'unavailable'}), 1)
        self.assertEqual(model.pose, 'tablet')
        self.assertEqual(model.detail, Detail.NONE)
        model.update(state(session_connected=False, supported=False), 2)
        self.assertEqual(model.detail, Detail.SESSION)
        model.update(state(target={'mode': 'unavailable'}, hardware=None), 3)
        self.assertEqual(model.detail, Detail.SWITCH)

    def test_settings_coalesce_failure_and_disconnect(self):
        client, errors, changes = Client(), [], []
        model = SettingsModel(client, lambda: changes.append(True), errors.append)
        model.update(state(manual='auto'))
        model.queue('manual', 'tent')
        model.queue('manual', 'stand')
        model.update(state(manual='auto'))
        self.assertEqual(model.value('manual'), 'stand')
        client.calls[0][2].set_exception(RuntimeError('old'))
        self.assertFalse(errors)
        self.assertEqual(client.calls[1][:2], ('mode', 'stand'))
        client.calls[1][2].set_exception(RuntimeError('current'))
        self.assertEqual(model.value('manual'), 'auto')
        self.assertEqual(len(errors), 1)
        model.queue('tent_enabled', False)
        old = client.calls[-1][2]
        model.disconnect()
        model.update(state())
        model.queue('tent_enabled', True)
        old.set_exception(RuntimeError('stale'))
        self.assertTrue(model.value('tent_enabled'))
        self.assertEqual(len(errors), 1)
        client.calls[-1][2].set_result(None)
        model.update(state(hardware=True, tent_enabled=False))
        self.assertFalse(model.mode_enabled('laptop'))
        self.assertFalse(model.preference_enabled('tent_prompt'))

    def test_synchronous_failure_does_not_stick(self):
        errors = []
        client = Client()
        def fail(value):
            raise OSError('transport')
        client.set_mode = fail
        model = SettingsModel(client, lambda: None, errors.append)
        model.update(state(manual='auto'))
        model.queue('manual', 'tent')
        self.assertEqual(model.value('manual'), 'auto')
        self.assertFalse(model.active)
        self.assertEqual(len(errors), 1)

    def test_action_dedup_and_old_connection(self):
        changes, errors = [], []
        runner = ActionRunner(lambda: changes.append(True), errors.append)
        old, new = Future(), Future()
        self.assertTrue(runner.run('confirm', lambda: old))
        self.assertFalse(runner.run('retry', lambda: new))
        runner.disconnect()
        runner.run('retry', lambda: new)
        old.set_exception(RuntimeError('old'))
        self.assertEqual(runner.busy, 'retry')
        self.assertFalse(errors)
        new.set_result(None)
        self.assertIsNone(runner.busy)

    def test_independent_settings_and_cancelled_request(self):
        client, errors = Client(), []
        model = SettingsModel(client, lambda: None, errors.append)
        model.update(state())
        model.queue('manual', 'stand')
        model.queue('tent_prompt', False)
        self.assertEqual(len(client.calls), 2)
        client.calls[0][2].cancel()
        self.assertEqual(model.active, {'tent_prompt'})
        self.assertEqual(len(errors), 1)
        client.calls[1][2].set_result(None)
        self.assertFalse(model.active)

    def test_disconnected_settings_do_not_write(self):
        client = Client()
        model = SettingsModel(client, lambda: None, lambda error: None)
        model.queue('manual', 'tent')
        self.assertFalse(client.calls)
        self.assertFalse(model.mode_enabled('tent'))
        self.assertFalse(model.preference_enabled('tent_enabled'))

    def test_widget_pages_do_not_encode_transport_messages(self):
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / 'src/panasonic_posture/ui'
        for filename in ('posture.py', 'settings.py'):
            tree = ast.parse((root / filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ('call', 'Variant'), filename)
