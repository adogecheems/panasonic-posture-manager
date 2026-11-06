"""Run the optional Windows frontend without loading Linux UI modules."""
import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Windows Fluent UI preview (backend pending).')
    parser.add_argument('--demo', action='store_true', help='enable in-memory interaction demo')
    parser.add_argument('--language', choices=('en', 'zh_CN'), help='interface language')
    parser.add_argument('--scenario', choices=('laptop', 'prompt', 'hardware', 'slow', 'error', 'disconnected'),
                        help='initial demo scenario (requires --demo)')
    options = parser.parse_args(argv)
    if options.scenario and not options.demo:
        parser.error('--scenario requires --demo')
    try:
        from .app import run
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith(('PyQt6', 'qfluentwidgets', 'qframelesswindow')):
            print('Install the optional UI dependencies from packaging/windows/requirements-ui.txt.', file=sys.stderr)
            return 2
        raise
    return run(demo=options.demo, language=options.language, scenario=options.scenario or 'laptop')


if __name__ == '__main__':
    raise SystemExit(main())
