"""Run the optional Windows frontend without loading Linux UI modules."""
import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Windows Fluent UI and software backend (device adapters pending).')
    parser.add_argument('--demo', action='store_true', help='enable in-memory interaction demo')
    parser.add_argument('--backend', action='store_true', help='start a private software backend (default on Windows)')
    parser.add_argument('--simulate-backend', action='store_true', help='simulate devices in the private backend')
    parser.add_argument('--settings', type=Path, help='explicit backend settings file')
    parser.add_argument('--language', choices=('en', 'zh_CN'), help='interface language')
    parser.add_argument('--scenario', choices=('laptop', 'prompt', 'hardware', 'slow', 'error', 'disconnected'),
                        help='initial simulation scenario (requires --demo or --simulate-backend)')
    options = parser.parse_args(argv)
    if options.demo and (options.backend or options.simulate_backend or options.settings):
        parser.error('--demo cannot be combined with backend options')
    if options.scenario and not (options.demo or options.simulate_backend):
        parser.error('--scenario requires --demo or --simulate-backend')
    if options.scenario == 'disconnected' and options.simulate_backend:
        parser.error('disconnected scenario is only available with --demo')
    if options.settings and not (options.backend or options.simulate_backend or sys.platform == 'win32'):
        parser.error('--settings requires --backend')
    if (options.backend or options.simulate_backend) and not options.settings and sys.platform != 'win32':
        parser.error('outside Windows, use --settings with an explicit test directory')
    try:
        from .app import run
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith(('PyQt6', 'qfluentwidgets', 'qframelesswindow')):
            print('Install the optional UI dependencies from packaging/windows/requirements-ui.txt.', file=sys.stderr)
            return 2
        raise
    return run(demo=options.demo, language=options.language, scenario=options.scenario or 'laptop',
               backend=options.backend, simulate_backend=options.simulate_backend, settings_path=options.settings)


if __name__ == '__main__':
    raise SystemExit(main())
