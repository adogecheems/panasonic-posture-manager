from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib

from panasonic_posture.session.screen_context import ScreenContext, desktop_context
from panasonic_posture.session.client import SystemClient
from panasonic_posture.session.failures import ServiceError


class ScreenContextTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.drm = Path(temp.name)
        self.context = ScreenContext(False, self.drm)

    def output(self, name, state):
        path = self.drm / name
        path.mkdir(exist_ok=True)
        (path / "status").write_text("connected")
        (path / "dpms").write_text(state)

    def test_monitor_off_and_on_edges(self):
        self.assertFalse(self.context())
        self.output("card0-eDP-1", "Off")
        self.assertTrue(self.context())
        self.output("card0-eDP-1", "On")
        self.assertFalse(self.context())

    def test_lit_external_output_and_unknown_state_do_not_bypass_prompt(self):
        self.output("card0-eDP-1", "Off")
        self.output("card0-DP-2", "On")
        self.assertFalse(self.context())
        self.output("card0-DP-2", "unknown")
        self.assertFalse(self.context())

    def test_mutter_power_save_overrides_stale_drm_until_unavailable(self):
        power = Mock(return_value=3)
        context = ScreenContext(False, self.drm, monitor_power=power)
        self.output("card0-eDP-1", "On")
        self.assertTrue(context())
        power.return_value = -1
        self.assertFalse(context())

    def test_greeter_adopts_current_posture_with_display_on(self):
        self.output("card0-eDP-1", "On")
        self.assertTrue(ScreenContext(True, self.drm)())

    def test_logind_greeter_uses_authenticated_unattended_method(self):
        bus = Mock()
        bus.call_sync.return_value = GLib.Variant("(v)", (GLib.Variant("s", "greeter"),))
        client = SystemClient(bus, screen_context=desktop_context(bus, Mock()))
        client.session_update(17, True)
        self.assertEqual(bus.call_sync.call_args.args[3], "SessionUpdateUnattended")
        self.assertEqual(bus.call_sync.call_args.args[4].unpack(), (17, True, "", ""))

    def test_legacy_fallback_only_when_method_is_missing(self):
        client = SystemClient(Mock(), screen_context=lambda: True)
        with patch.object(client, "_call", side_effect=[
            ServiceError(name="org.freedesktop.DBus.Error.UnknownMethod"), None,
        ]) as call:
            client.session_update(17, False)
            self.assertEqual([args.args[0] for args in call.call_args_list],
                             ["SessionUpdateUnattended", "SessionUpdate"])
        with patch.object(client, "_call", side_effect=ServiceError("access-denied")) as call:
            with self.assertRaises(ServiceError):
                client.session_update(17, False)
            call.assert_called_once()
