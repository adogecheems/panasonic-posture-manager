"""Run the optional Windows frontend without loading Linux UI modules."""
import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Windows Fluent UI with the hardware service by default.')
    parser.add_argument('--background', action='store_true', help='start the default hardware UI in the system tray')
    parser.add_argument('--language', choices=('en', 'zh_CN'), help='interface language')
    options = parser.parse_args(argv)
    if options.background and sys.platform != 'win32':
        parser.error('--background requires Windows')
    try:
        from .app import run
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith(('PyQt6', 'qfluentwidgets', 'qframelesswindow')):
            print('Install the optional UI dependencies from packaging/windows/dependencies/requirements-ui.txt.', file=sys.stderr)
            return 2
        raise
    return run(language=options.language, background=options.background)


if __name__ == '__main__':
    raise SystemExit(main())
