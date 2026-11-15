"""Exercise process signal sources with real GI and isolated collaborators."""

import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._test_support import require_linux

require_linux()

from gi.repository import Gio, GLib
from panasonic_posture.linux.service.daemon import Daemon
from panasonic_posture.linux.session.agent import Agent


class SignalLifecycleTests(unittest.TestCase):
    def test_processes_register_and_release_native_signal_sources(self):
        for process in ('service', 'session'):
            with self.subTest(process=process), tempfile.TemporaryDirectory() as directory:
                with (mock.patch.object(Gio, 'bus_own_name', return_value=1),
                      mock.patch.object(Gio, 'bus_unown_name'),
                      mock.patch('panasonic_posture.linux.service.daemon.ServiceApplication.create')):
                    loop = mock.Mock()
                    if process == 'service':
                        owner = Daemon(Gio.BusType.SESSION, Path(directory) / 'settings.json')
                        owner.loop = loop
                        run = owner.run
                        stop = owner._quit
                    else:
                        owner = Agent(system_bus=object(), uid=1000,
                                      own_name=mock.Mock(return_value=1),
                                      unown_name=mock.Mock(), loop_factory=lambda: loop)
                        run = owner.serve
                        stop = owner._on_signal
                    sources = []

                    def run_loop():
                        sources.extend(owner._signal_sources)
                        self.assertEqual(len(sources), 2)
                        for source in sources:
                            self.assertIsNotNone(GLib.MainContext.default().find_source_by_id(source))
                        # Explicit cleanup owns removal even after a signal fires.
                        self.assertTrue(stop())
                        loop.quit.assert_called_once()

                    loop.run.side_effect = run_loop
                    with mock.patch.object(GLib, 'unix_signal_add', wraps=GLib.unix_signal_add) as install:
                        self.assertEqual(run(), 0)
                    self.assertEqual([call.args[1] for call in install.call_args_list],
                                     [signal.SIGTERM, signal.SIGINT])
                    self.assertEqual(owner._signal_sources, [])
                    for source in sources:
                        self.assertIsNone(GLib.MainContext.default().find_source_by_id(source))


if __name__ == '__main__':
    unittest.main()
