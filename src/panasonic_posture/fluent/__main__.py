"""Run the optional Windows frontend without loading Linux UI modules."""
import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Windows Fluent UI with the hardware service by default.')
    parser.add_argument('--demo', action='store_true', help='enable in-memory interaction demo')
    parser.add_argument('--backend', action='store_true', help='start the private software backend preview')
    parser.add_argument('--background', action='store_true', help='start the default hardware UI in the system tray')
    parser.add_argument('--settings', type=Path, help='explicit backend settings file')
    parser.add_argument('--language', choices=('en', 'zh_CN'), help='interface language')
    parser.add_argument('--scenario', choices=('laptop', 'prompt', 'hardware', 'slow', 'error', 'disconnected'),
                        help='initial demo scenario (requires --demo)')
    options = parser.parse_args(argv)
    if options.background and (options.demo or options.backend or options.settings or options.scenario):
        parser.error('--background cannot be combined with preview backend options')
    if options.background and sys.platform != 'win32':
        parser.error('--background requires Windows')
    if options.demo and (options.backend or options.settings):
        parser.error('--demo cannot be combined with backend options')
    if options.scenario and not options.demo:
        parser.error('--scenario requires --demo')
    if options.settings and not options.backend:
        parser.error('--settings requires --backend')
    if options.backend and not options.settings and sys.platform != 'win32':
        parser.error('outside Windows, use --settings with an explicit test directory')
    try:
        from .app import run
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith(('PyQt6', 'qfluentwidgets', 'qframelesswindow')):
            print('Install the optional UI dependencies from packaging/windows/requirements-ui.txt.', file=sys.stderr)
            return 2
        raise
    return run(demo=options.demo, language=options.language, scenario=options.scenario or 'laptop',
               backend=options.backend, settings_path=options.settings, background=options.background)


if __name__ == '__main__':
    raise SystemExit(main())
