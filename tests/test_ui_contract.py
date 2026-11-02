"""JS <-> Python contract tests for the GJS UI and the shared protocol.

The static checks always run and only need the standard library.  The dynamic
checks spawn ``gjs`` (and optionally a headless ``gtk4-broadwayd``) and are
skipped when the tools are unavailable, so ``make check`` stays green on
machines without the GNOME runtime while still guarding the contract on the
target system.

Covered regressions (QA-session-ui / QA-core):
* protocol.ErrorCode <-> session/errors.py <-> data/ui/errors.js parity;
* STATE_VERSION / POSES / MODES / hardware-status / backend constants parity;
* every ``state.<field>`` the UI reads exists in ``StateSnapshot.to_dict()``;
* ``Retry()`` is exported by the service and used by the retry button;
* the session adapter owns the per-uid bus name (``session_name(uid)``);
* the D-Bus name is valid and differs from the deprecated machine-wide name;
* headless GTK build of both pages, local ``--settings`` start page, error
  text mapping (service unavailable / access denied / no detail leakage);
* the illustration self-check stays green.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SRC = str(REPO / "src")
if _SRC not in sys.path:  # make check already sets PYTHONPATH=src
    sys.path.insert(0, _SRC)

from panasonic_posture import protocol  # noqa: E402
from panasonic_posture.model import StateSnapshot  # noqa: E402
from panasonic_posture.session import errors as session_errors  # noqa: E402

GJS = shutil.which("gjs")
BROADWAY = shutil.which("gtk4-broadwayd")
UI_DIR = REPO / "data" / "ui"


def read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _js_uri(relative: str) -> str:
    return (REPO / relative).as_uri()


def run_gjs(source: str, *, env: dict | None = None, timeout: float = 40.0):
    """Run a GJS ESM script and return the completed process."""

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    )
    try:
        handle.write(source)
        handle.close()
        run_env = os.environ.copy()
        run_env.update(
            {
                "LC_ALL": "C",
                "LANG": "C",
                "LANGUAGE": "C",
                "PANASONIC_POSTURE_LOCALE_DIR": "/nonexistent-locale",
            }
        )
        if env:
            run_env.update(env)
        return subprocess.run(
            [GJS, "-m", handle.name],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Static protocol <-> UI parity
# ---------------------------------------------------------------------------


class ProtocolUiParityTests(unittest.TestCase):
    def test_error_code_lists_are_identical(self):
        expected = set(protocol.ALL_ERROR_CODES)
        js_codes = set(
            re.findall(
                r"^\s*'([a-z][a-z0-9-]+)':\s*_\(", read("data/ui/errors.js"), re.M
            )
        )
        python_codes = set(session_errors.ERROR_MESSAGES)
        self.assertEqual(js_codes, expected)
        self.assertEqual(python_codes, expected)

    def test_error_texts_are_identical_in_python_and_js(self):
        python_source = read("src/panasonic_posture/session/errors.py")
        python_map = {
            getattr(protocol.ErrorCode, name): msgid
            for name, msgid in re.findall(
                r"ErrorCode\.([A-Z_]+):\s*_\(\s*\"([^\"]+)\"", python_source, re.S
            )
        }
        js_map = {
            code: msgid
            for code, msgid in re.findall(
                r"'([a-z][a-z0-9-]+)':\s*_\(\s*'([^']+)'", read("data/ui/errors.js")
            )
        }
        self.assertEqual(python_map, js_map)

    def test_access_denied_is_localized(self):
        self.assertIn("access-denied", protocol.ALL_ERROR_CODES)
        po = read("po/zh_CN.po")
        self.assertIn(
            'msgid "Please switch back to the local desktop and try again."\n'
            'msgstr "请切回本机桌面后重试。"',
            po,
        )

    def test_state_version_matches_protocol(self):
        match = re.search(r"export const STATE_VERSION = (\d+);", read("data/ui/service.js"))
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), protocol.STATE_VERSION)

    def test_pose_whitelist_matches_protocol(self):
        match = re.search(r"export const POSES = \[([^\]]+)\]", read("data/ui/illustration.js"))
        self.assertIsNotNone(match)
        poses = re.findall(r"'([a-z]+)'", match.group(1))
        self.assertEqual(poses, list(protocol.POSES))

    def test_mode_list_matches_protocol(self):
        keys = re.findall(
            r"\['(auto|laptop|tent|stand|tablet)',", read("data/ui/settingsPage.js")
        )
        self.assertEqual(tuple(keys), tuple(protocol.MODES))

    def test_posture_page_only_uses_user_facing_state(self):
        # The caption under the posture drawing must show only translated
        # errors or a necessary readiness message.  Hardware/sensor/profile
        # details live in GetState and the logs.
        page = read("data/ui/posturePage.js")
        for technical in (
            "state.hardware_status",
            "state.hardware",
            "state.profile",
            "state.sensor_error",
            "state.sensor_source",
            "state.backend",
            "state.model",
            "state.vendor",
        ):
            self.assertNotIn(technical, page, f"posture page exposes {technical}")
        for text in (
            "panasonic-posture-manager-probe",
            "Generic Panasonic profile",
            "Sensor data",
            "First use",
            "tablet switch is on",
        ):
            self.assertNotIn(text, page)

    def test_mode_descriptions_are_user_facing(self):
        settings = read("data/ui/settingsPage.js")
        for technical in (
            "Internal input disabled",
            "inverted landscape",
            "auto rotation",
            "Keyboard enabled",
        ):
            self.assertNotIn(technical, settings)

    def test_settings_about_exposes_author_and_repository(self):
        settings = read("data/ui/settingsPage.js")
        self.assertIn("export const AUTHOR_NAME = 'adogecheems';", settings)
        self.assertIn(
            "export const GITHUB_URL = "
            "'https://github.com/adogecheems/panasonic-posture-manager';",
            settings,
        )
        self.assertIn("new Gtk.LinkButton", settings)

    def test_ui_state_fields_exist_in_the_snapshot(self):
        known = set(StateSnapshot().to_dict())
        used = set()
        for path in sorted(UI_DIR.glob("*.js")):
            used.update(
                re.findall(r"(?<![A-Za-z_])state\.([a-z_]+)", path.read_text(encoding="utf-8"))
            )
        self.assertTrue(used)
        self.assertLessEqual(used, known, f"unknown UI state fields: {sorted(used - known)}")

    def test_session_adapter_uses_the_per_uid_bus_name(self):
        source = read("src/panasonic_posture/session/agent.py")
        self.assertIn("protocol.session_name(self.uid)", source)
        self.assertNotIn("protocol.SESSION_NAME", source)
        self.assertIn("os.getuid()", source)

    def test_per_uid_session_name_is_a_valid_dbus_name(self):
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except (ImportError, ValueError):  # pragma: no cover - PyGObject missing
            self.skipTest("PyGObject is not available")
        seen = set()
        for uid in (0, 1000, 65534):
            name = protocol.session_name(uid)
            self.assertNotIn(name, seen)
            seen.add(name)
            self.assertTrue(name.startswith(protocol.SESSION_NAME_PREFIX))
            self.assertNotEqual(name, protocol.SESSION_NAME)
            self.assertTrue(Gio.dbus_is_name(name), f"invalid D-Bus name {name}")
        self.assertEqual(protocol.session_name(1000), protocol.session_name(1000))

    def test_retry_method_is_exposed_and_used(self):
        self.assertIn('<method name="Retry"/>', protocol.DBUS_XML)
        service = read("data/ui/service.js")
        self.assertIn("await this.call('Retry')", service)
        self.assertIn("isUnknownMethodError(error)", service)
        self.assertIn("await this.setMode(fallbackMode)", service)
        page = read("data/ui/posturePage.js")
        self.assertIn("this._service.retry(fallbackMode)", page)
        self.assertIn("this._retryButton.connect('clicked', () => this._retry())", page)

    def test_command_line_handling_is_implemented(self):
        app = read("data/ui/app.js")
        self.assertIn("Gio.ApplicationFlags.HANDLES_COMMAND_LINE", app)
        self.assertIn("connect('command-line'", app.replace('"', "'"))
        self.assertIn("requestedPageFromCommandLine", app)
        self.assertIn("get_arguments", app)
        # --settings must stay a positional argument so GApplication forwards
        # it to the primary instance (remote command lines carry no options).
        self.assertNotIn("add_main_option", app)

    def test_application_icon_is_configured_and_packaged(self):
        app = read("data/ui/app.js")
        self.assertIn("Gtk.Window.set_default_icon_name(APPLICATION_ID)", app)
        self.assertIn("window.set_icon_name(APPLICATION_ID)", app)
        self.assertIn("theme.add_search_path(ASSETS_DIR)", app)

        desktop = read("packaging/desktop/work.mmoe.panasonic.PostureManager.desktop")
        self.assertIn("Icon=work.mmoe.panasonic.PostureManager", desktop)

        icon = REPO / "data/assets/work.mmoe.panasonic.PostureManager.png"
        self.assertTrue(icon.is_file())
        header = icon.read_bytes()[:24]
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(header[16:20], "big"), 512)
        self.assertEqual(int.from_bytes(header[20:24], "big"), 512)

        pkgbuild = read("packaging/aur/PKGBUILD")
        self.assertIn(
            "/usr/share/icons/hicolor/512x512/apps/work.mmoe.panasonic.PostureManager.png",
            pkgbuild,
        )

    def test_bottom_navigation_does_not_mimic_selection_on_hover(self):
        app = read("data/ui/app.js")
        navigation = read("data/ui/navigation.js")
        self.assertIn("createNavigationBar(stack)", app)
        self.assertIn("button.toggle:hover:not(:checked)", navigation)
        self.assertIn("background-color: transparent", navigation)
        self.assertIn("box-shadow: none", navigation)


# ---------------------------------------------------------------------------
# Dynamic JavaScript checks
# ---------------------------------------------------------------------------


def _gjs_probe(modules: list[str]) -> subprocess.CompletedProcess | None:
    if not GJS:
        return None
    source = "".join(
        f"import '{module}';\n" if module == "gettext" else f"import 'gi://{module}';\n"
        for module in modules
    )
    source += "print('probe-ok');\n"
    try:
        return run_gjs(source, timeout=30)
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - broken env
        return None


class JavaScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = _gjs_probe(["Gio", "GLib", "GdkPixbuf?version=2.0", "gettext"])
        if probe is None or probe.returncode != 0:
            raise unittest.SkipTest("gjs with the GIO/gettext typelibs is not usable")

    def test_parse_state_and_error_mapping(self):
        sample = json.dumps(StateSnapshot(generation=1, instance="i").to_dict())
        source = f"""
import System from 'system';
import {{parseState, STATE_VERSION, serviceUnavailableError}} from {json.dumps(_js_uri('data/ui/service.js'))};
import {{errorText, errorCodeFromMessage, describeError, isAccessDenied,
        isUnknownMethodError}} from {json.dumps(_js_uri('data/ui/errors.js'))};

const failures = [];
const check = (condition, message) => {{ if (!condition) failures.push(message); }};
const sample = {json.dumps(sample)};

check(STATE_VERSION === {protocol.STATE_VERSION}, 'STATE_VERSION');
check(parseState(sample).generation === 1, 'parse a v2 snapshot');
for (const bad of ['[]', '{{"version":1}}', '{{"version":2}}',
                   '{{"version":2,"generation":"x","target":{{}}}}',
                   '{{"version":2,"generation":1,"target":5}}']) {{
    let accepted = false;
    try {{ parseState(bad); accepted = true; }} catch (error) {{ }}
    check(!accepted, 'parseState accepted ' + bad);
}}
check(errorCodeFromMessage(
    'GDBus.Error:work.mmoe.panasonic.Posture.Error: display-transform-not-applied: x')
    === 'display-transform-not-applied', 'code from D-Bus message');
check(errorCodeFromMessage('plain text') === null, 'no code from plain text');
check(errorText('access-denied') === 'Please switch back to the local desktop and try again.',
      'access-denied text: ' + errorText('access-denied'));
check(describeError(serviceUnavailableError()) === 'Background service unavailable',
      'unavailable text: ' + describeError(serviceUnavailableError()));
check(describeError(serviceUnavailableError()) !== describeError(new Error('boom')),
      'unavailable must differ from the fallback');
check(isAccessDenied(new Error(
    'GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: nope')), 'access denied detection');
check(isUnknownMethodError(new Error(
    'GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: nope')), 'unknown method detection');
check(!isUnknownMethodError(new Error(
    'GDBus.Error:org.freedesktop.DBus.Error.NoReply: nope')), 'NoReply is not UnknownMethod');
check(describeError(new Error(
    'GDBus.Error:org.freedesktop.DBus.Error.NoReply: nope')) === errorText('internal-error'),
    'NoReply falls back to internal-error');
if (failures.length) {{
    printerr(failures.join('; '));
    System.exit(1);
}}
System.exit(0);
"""
        result = run_gjs(source)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_retry_falls_back_to_set_mode_for_older_services(self):
        source = f"""
import GLib from 'gi://GLib';
import System from 'system';
import {{PostureService}} from {json.dumps(_js_uri('data/ui/service.js'))};

const failures = [];
const check = (condition, message) => {{ if (!condition) failures.push(message); }};

function fakeService(handler) {{
    const proxy = {{
        calls: [],
        handler,
        call(method, _params, _flags, _timeout, _cancellable, callback) {{
            this.calls.push(method);
            callback(this, {{method}});
        }},
        call_finish(result) {{
            const error = this.handler(result.method);
            if (error)
                throw error;
            return {{unpack: () => []}};
        }},
    }};
    const service = new PostureService(null);
    service._proxy = proxy;
    return {{service, proxy}};
}}

const unknownMethod = new Error(
    'GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: no Retry here');
const {{service, proxy}} = fakeService(
    method => method === 'Retry' ? unknownMethod : null);
await service.retry('tent');
check(proxy.calls.join(',') === 'Retry,SetMode',
      'fallback order was ' + proxy.calls.join(','));

const {{service: fatalService, proxy: fatalProxy}} = fakeService(
    method => method === 'Retry'
        ? new Error('GDBus.Error:org.freedesktop.DBus.Error.NoReply: slow')
        : null);
let rejected = false;
try {{
    await fatalService.retry('tent');
}} catch (error) {{
    rejected = true;
}}
check(rejected, 'non-UnknownMethod error must not fall back');
check(fatalProxy.calls.join(',') === 'Retry',
      'unexpected methods after fatal error: ' + fatalProxy.calls.join(','));

if (failures.length) {{
    printerr(failures.join('; '));
    System.exit(1);
}}
System.exit(0);
"""
        result = run_gjs(source)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_illustration_self_check(self):
        # `gjs -m data/ui/illustration.js` runs the asset self-check and must be
        # completely silent on success (QA: it was never executed by CI before).
        environment = os.environ.copy()
        environment.update(
            {"LC_ALL": "C", "LANG": "C", "G_MESSAGES_DEBUG": "",
             "PANASONIC_POSTURE_LOCALE_DIR": "/nonexistent-locale"}
        )
        completed = subprocess.run(
            [GJS, "-m", str(UI_DIR / "illustration.js")],
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")


class HeadlessUiTests(unittest.TestCase):
    """Real GTK4/libadwaita build of both pages via the Broadway backend."""

    @classmethod
    def setUpClass(cls):
        if not GJS or not BROADWAY:
            raise unittest.SkipTest("gjs and gtk4-broadwayd are required")
        probe = _gjs_probe(["Gtk?version=4.0", "Adw?version=1", "Gio", "GLib"])
        if probe is None or probe.returncode != 0:
            raise unittest.SkipTest("headless GTK4/libadwaita is not usable")

    def setUp(self):
        self._runtime = tempfile.TemporaryDirectory(prefix="posture-broadway-")
        self.addCleanup(self._runtime.cleanup)
        environment = os.environ.copy()
        environment.update({"XDG_RUNTIME_DIR": self._runtime.name, "LC_ALL": "C"})
        self._daemon = subprocess.Popen(
            [BROADWAY, ":99"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )

        def stop_daemon():
            if self._daemon.poll() is None:
                self._daemon.terminate()
                try:
                    self._daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    self._daemon.kill()
            if self._daemon.stdout is not None:
                self._daemon.stdout.close()

        self.addCleanup(stop_daemon)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if any(Path(self._runtime.name).glob("*.socket")):
                break
            if self._daemon.poll() is not None:
                self.skipTest("gtk4-broadwayd failed to start")
            time.sleep(0.05)
        else:
            self.skipTest("gtk4-broadwayd did not create a socket")
        self._environment = {
            "XDG_RUNTIME_DIR": self._runtime.name,
            "GDK_BACKEND": "broadway",
            "BROADWAY_DISPLAY": ":99",
        }

    def test_both_pages_build_and_settings_command_line_works(self):
        source = f"""
import GLib from 'gi://GLib';
import Gdk from 'gi://Gdk?version=4.0';
import Gtk from 'gi://Gtk?version=4.0';
import System from 'system';
import {{createApplication, currentUi, requestedPageFromCommandLine,
        applyRequestedPage}} from {json.dumps(_js_uri('data/ui/app.js'))};

const app = createApplication('work.mmoe.panasonic.PostureManager.HeadlessTest');
const failures = [];

function findWidget(widget, cssName) {{
    if (widget.get_css_name && widget.get_css_name() === cssName)
        return widget;
    let child = widget.get_first_child ? widget.get_first_child() : null;
    while (child) {{
        const found = findWidget(child, cssName);
        if (found)
            return found;
        child = child.get_next_sibling();
    }}
    return null;
}}

GLib.timeout_add(GLib.PRIORITY_DEFAULT, 900, () => {{
    const ui = currentUi();
    if (!ui) {{
        failures.push('window was not built');
    }} else {{
        const switcherBar = findWidget(ui.window, 'viewswitcherbar');
        if (!switcherBar || !switcherBar.has_css_class('posture-navigation'))
            failures.push('bottom navigation style class missing');
        try {{
            const display = Gdk.Display.get_default();
            const theme = Gtk.IconTheme.get_for_display(display);
            if (!theme.has_icon('work.mmoe.panasonic.PostureManager'))
                failures.push('application icon was not found');
        }} catch (error) {{
            failures.push('application icon check failed: ' + error.message);
        }}
        if (ui.stack.visible_child_name !== 'settings')
            failures.push('--settings opened ' + ui.stack.visible_child_name);
        const commandLine = {{get_arguments: () => ['program', '--settings']}};
        if (requestedPageFromCommandLine(commandLine) !== 'settings')
            failures.push('command-line helper failed');
        const empty = {{get_arguments: () => ['program']}};
        if (requestedPageFromCommandLine(empty) !== null)
            failures.push('default page helper failed');
        applyRequestedPage('posture');
        if (ui.stack.visible_child_name !== 'posture')
            failures.push('applyRequestedPage failed');
        // Exercise every rendering branch of the pages with a v2 state.
        const target = {{mode: 'tent', tablet: true, rotation: 180,
                         disable_internal_input: true}};
        const state = {{
            version: 2, instance: 'qa', generation: 4, pose: 'tent', hardware: true,
            hardware_status: 'ambiguous', manual: 'auto', tent_prompt: true,
            target, applied: target, pending_notification: 4, backend: 'ready',
            error_code: 'access-denied', error_detail: 'inactive', session_connected: true,
            supported: true, sensor_source: 'iio', sensor_error: null,
            profile: 'generic', model: 'CFQV8', vendor: 'Panasonic',
        }};
        ui.posturePage.update(state);
        ui.settingsPage.update(state);
        ui.posturePage.setUnavailable();
        ui.settingsPage.setUnavailable();
    }}
    if (failures.length) {{
        printerr(failures.join('; '));
        System.exit(1);
    }}
    print('ui contract self-check passed');
    app.quit();
    return GLib.SOURCE_REMOVE;
}});
System.exit(app.run([System.programInvocationName, '--settings']));
"""
        result = run_gjs(source, env=self._environment, timeout=60)
        self.assertEqual(result.returncode, 0, (result.stderr or "") + (result.stdout or ""))
        self.assertIn("ui contract self-check passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
