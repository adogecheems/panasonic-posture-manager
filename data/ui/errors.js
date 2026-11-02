// Localized text for the stable error codes reported by the privileged
// service.  This mirrors src/panasonic_posture/session/errors.py; the detail
// string of an error is never shown (P2-18).
//
// tests/check_i18n.py verifies that every literal below is covered by
// po/zh_CN.po, so a new code must always get its message and translation.
// tests/test_ui_contract.py verifies the code list matches protocol.ErrorCode.
import {_} from './i18n.js';

const MESSAGES = {
    'invalid-mode': _('This mode is not available right now.'),
    'access-denied': _('Please switch back to the local desktop and try again.'),
    'unsupported-model': _('This model does not support automatic mode changes.'),
    'hardware-priority': _('The tablet switch currently controls the mode.'),
    'expired-confirmation': _('The tent prompt has expired. Wait for it to appear again.'),
    'config-write-failed': _('The setting could not be saved. Please try again.'),
    'relay-start-failed': _('The internal keyboard and touchpad could not be controlled. Please try again.'),
    'relay-exited': _('Internal input control stopped. Please try again.'),
    'relay-heartbeat-timeout': _('Internal input control stopped responding. Please try again.'),
    'relay-input-grab-failed': _('The internal keyboard and touchpad could not be disabled. Please try again.'),
    'native-state-recovery-failed': _('The tablet switch state could not be restored. Restart the service if input behaves unexpectedly.'),
    'sensor-timeout': _('No posture data is available right now.'),
    'display-mirrored-or-disabled': _('This screen arrangement cannot be rotated. Please adjust the display settings.'),
    'display-layout-conflict': _('The external display arrangement does not allow this mode.'),
    'display-transform-not-applied': _('The screen rotation could not be applied. Please try again.'),
    'display-rotation-lock-failed': _('The rotation lock could not be changed. Please try again.'),
    'display-unavailable': _('Screen rotation is not available right now.'),
    'display-unknown-mode': _('This display mode is not supported.'),
    'display-internal-monitor-changed': _('The internal screen changed while applying the mode. Please try again.'),
    'internal-error': _('Something went wrong. Please try again.'),
};

const FALLBACK = 'internal-error';

//: JS-only marker for "there is no usable D-Bus proxy right now".  It is not
//: a protocol error code; it lets pages show the connection message instead
//: of "unexpected error" (P3).
export const SERVICE_UNAVAILABLE_CODE = 'service-unavailable';

const SERVICE_UNAVAILABLE_MARKERS = /ServiceUnknown|NameHasNoOwner|No such name|no usable bus|service unavailable/i;
const ACCESS_DENIED_MARKERS = /AccessDenied|AuthFailed|NotAuthorized|access-denied/i;
const UNKNOWN_METHOD_MARKERS = /UnknownMethod|No such method|unknown method/i;

function messageOf(error) {
    if (error && typeof error === 'object' && 'message' in error)
        return String(error.message ?? '');
    return String(error ?? '');
}

export function errorText(code) {
    const key = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? code : FALLBACK;
    return MESSAGES[key];
}

// The D-Bus error message has the form
//     GDBus.Error:<error name>: <stable code>: <detail>
// and the UI only ever shows the translated stable code.
export function errorCodeFromMessage(message) {
    let text = String(message ?? '');
    const marker = text.indexOf('GDBus.Error:');
    if (marker >= 0)
        text = text.slice(marker + 'GDBus.Error:'.length);
    const parts = text.split(':');
    let code = (parts[0] ?? '').trim();
    if (code.includes('.') && parts.length > 1)
        code = (parts[1] ?? '').trim();
    return Object.prototype.hasOwnProperty.call(MESSAGES, code) ? code : null;
}

//: True when the bus name / connection is unavailable (service down).
export function isServiceUnavailable(error) {
    if (error && typeof error === 'object' && error.code === SERVICE_UNAVAILABLE_CODE)
        return true;
    return SERVICE_UNAVAILABLE_MARKERS.test(messageOf(error));
}

//: True when the request was rejected because no active local session exists.
export function isAccessDenied(error) {
    return ACCESS_DENIED_MARKERS.test(messageOf(error));
}

//: True when a D-Bus method does not exist (older service without Retry()).
export function isUnknownMethodError(error) {
    return UNKNOWN_METHOD_MARKERS.test(messageOf(error));
}

export function describeError(error) {
    if (isServiceUnavailable(error))
        return _('Background service unavailable');
    if (isAccessDenied(error))
        return errorText('access-denied');
    const code = errorCodeFromMessage(messageOf(error));
    return code ? errorText(code) : errorText(FALLBACK);
}
