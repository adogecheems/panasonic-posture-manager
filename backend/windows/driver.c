/* SPDX-License-Identifier: AGPL-3.0-or-later
 * DEVELOPMENT DRAFT: not compiled or hardware-validated. Excluded from all
 * packages and default builds. Do not install or register this filter.
 *
 * Opt-in KMDF filter for exactly configured internal keyboard/mouse-class
 * devices. No installer or class-wide registration is supplied. Unconfigured,
 * external, HID precision-touchpad and ambiguous devices remain untouched.
 *
 * Input packets are forwarded unchanged or discarded in kernel space. Only
 * ephemeral pressed-state bits establish the idle boundary; input contents,
 * coordinates and pressed-state maps are never logged or exported.
 */
#include <ntddk.h>
#include <wdf.h>
#include <ntddkbd.h>
#include <ntddmou.h>
#include <kbdmou.h>
#include <initguid.h>
#include <devguid.h>
#include <devpkey.h>
#include <wdmguid.h>
#include "control.h"

#define IOCTL_POSTURE_CONTROL 0x0022e000u
#define ROLE_KEYBOARD 0
#define ROLE_TOUCHPAD 1

typedef struct {
    CONNECT_DATA Upper;
    int Role;
    BOOLEAN Registered, Connected, Powered, UnknownKeys;
    UCHAR Keys[3][32];
    ULONG HeldKeys, Buttons;
} DEVICE_CONTEXT;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(DEVICE_CONTEXT, DeviceContext)

typedef struct { struct posture_connection Connection; } FILE_CONTEXT;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(FILE_CONTEXT, FileContext)

static struct {
    WDFDEVICE Control;
    WDFSPINLOCK Lock;
    WDFTIMER Timer;
    WDFSTRING KeyboardId, TouchpadId;
    BOOLEAN Configured;
    DEVICE_CONTEXT *Devices[2];
    ULONG Counts[2], InFlight;
    struct posture_guard Guard;
} Global;

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD PostureDeviceAdd;
EVT_WDF_DRIVER_UNLOAD PostureUnload;
EVT_WDF_IO_QUEUE_IO_INTERNAL_DEVICE_CONTROL PostureInternal;
EVT_WDF_IO_QUEUE_IO_DEVICE_CONTROL PostureControl;
EVT_WDF_REQUEST_COMPLETION_ROUTINE PostureConnected;
EVT_WDF_DEVICE_FILE_CREATE PostureFileCreate;
EVT_WDF_FILE_CLEANUP PostureFileCleanup;
EVT_WDF_TIMER PostureWatchdog;
EVT_WDF_DEVICE_D0_ENTRY PostureD0Entry;
EVT_WDF_DEVICE_D0_EXIT PostureD0Exit;
EVT_WDF_DEVICE_SURPRISE_REMOVAL PostureRemoved;
EVT_WDF_OBJECT_CONTEXT_CLEANUP PostureDeviceCleanup;

static uint64_t now_ms(void) { return KeQueryInterruptTime() / 10000u; }

/* All guard/state helpers run under Global.Lock. No allocation or I/O there. */
static void update_state(void) {
    ULONG role;
    Global.Guard.ready_mask = 0;
    Global.Guard.busy_mask = Global.InFlight ? 4u : 0u;
    for (role = 0; role < 2; ++role) {
        DEVICE_CONTEXT *context = Global.Devices[role];
        if (Global.Counts[role] != 1 || context == NULL ||
            !context->Connected || !context->Powered)
            continue;
        Global.Guard.ready_mask |= 1u << role;
        if ((role == ROLE_KEYBOARD && (context->HeldKeys || context->UnknownKeys)) ||
            (role == ROLE_TOUCHPAD && context->Buttons))
            Global.Guard.busy_mask |= 1u << role;
    }
    (void)posture_blocked(&Global.Guard, now_ms());
}

static void track_keys(DEVICE_CONTEXT *context, PKEYBOARD_INPUT_DATA start,
                       PKEYBOARD_INPUT_DATA end) {
    while (start < end) {
        ULONG bank = (start->Flags & KEY_E1) ? 2u : (start->Flags & KEY_E0) ? 1u : 0u;
        ULONG code = start->MakeCode;
        if (code == 0 || code >= 256 || (start->Flags & (KEY_E0 | KEY_E1)) == (KEY_E0 | KEY_E1)) {
            context->UnknownKeys = TRUE;
        } else {
            UCHAR mask = (UCHAR)(1u << (code % 8));
            UCHAR *bits = &context->Keys[bank][code / 8];
            if (start->Flags & KEY_BREAK) {
                if (*bits & mask) { *bits &= (UCHAR)~mask; --context->HeldKeys; }
            } else if (!(*bits & mask)) {
                *bits |= mask;
                ++context->HeldKeys;
            }
        }
        ++start;
    }
}

static void track_buttons(DEVICE_CONTEXT *context, PMOUSE_INPUT_DATA start,
                          PMOUSE_INPUT_DATA end) {
    while (start < end) {
        ULONG index;
        for (index = 0; index < 5; ++index) {
            if (start->ButtonFlags & (1u << (index * 2))) context->Buttons |= 1u << index;
            if (start->ButtonFlags & (2u << (index * 2))) context->Buttons &= ~(1u << index);
        }
        ++start;
    }
}

static VOID keyboard_service(PDEVICE_OBJECT device, PKEYBOARD_INPUT_DATA start,
                             PKEYBOARD_INPUT_DATA end, PULONG consumed) {
    DEVICE_CONTEXT *context = DeviceContext(WdfWdmDeviceGetWdfDeviceHandle(device));
    PSERVICE_CALLBACK_ROUTINE upper;
    PDEVICE_OBJECT upper_device;
    ULONG before = *consumed;
    WdfSpinLockAcquire(Global.Lock);
    update_state();
    if (posture_blocked(&Global.Guard, now_ms())) {
        track_keys(context, start, end);
        *consumed += (ULONG)(end - start);
        update_state();
        WdfSpinLockRelease(Global.Lock);
        return;
    }
    ++Global.InFlight;
    update_state();
    upper = (PSERVICE_CALLBACK_ROUTINE)context->Upper.ClassService;
    upper_device = context->Upper.ClassDeviceObject;
    WdfSpinLockRelease(Global.Lock);
    upper(upper_device, start, end, consumed);
    WdfSpinLockAcquire(Global.Lock);
    if (*consumed >= before && *consumed - before <= (ULONG)(end - start))
        track_keys(context, start, start + (*consumed - before));
    else
        context->UnknownKeys = TRUE;
    --Global.InFlight;
    update_state();
    WdfSpinLockRelease(Global.Lock);
}

static VOID mouse_service(PDEVICE_OBJECT device, PMOUSE_INPUT_DATA start,
                          PMOUSE_INPUT_DATA end, PULONG consumed) {
    DEVICE_CONTEXT *context = DeviceContext(WdfWdmDeviceGetWdfDeviceHandle(device));
    PSERVICE_CALLBACK_ROUTINE upper;
    PDEVICE_OBJECT upper_device;
    ULONG before = *consumed;
    WdfSpinLockAcquire(Global.Lock);
    update_state();
    if (posture_blocked(&Global.Guard, now_ms())) {
        track_buttons(context, start, end);
        *consumed += (ULONG)(end - start);
        update_state();
        WdfSpinLockRelease(Global.Lock);
        return;
    }
    ++Global.InFlight;
    update_state();
    upper = (PSERVICE_CALLBACK_ROUTINE)context->Upper.ClassService;
    upper_device = context->Upper.ClassDeviceObject;
    WdfSpinLockRelease(Global.Lock);
    upper(upper_device, start, end, consumed);
    WdfSpinLockAcquire(Global.Lock);
    if (*consumed >= before && *consumed - before <= (ULONG)(end - start))
        track_buttons(context, start, start + (*consumed - before));
    else
        context->Buttons = MAXULONG; /* impossible completion: never claim idle */
    --Global.InFlight;
    update_state();
    WdfSpinLockRelease(Global.Lock);
}

VOID PostureConnected(WDFREQUEST request, WDFIOTARGET target,
                      PWDF_REQUEST_COMPLETION_PARAMS parameters, WDFCONTEXT raw) {
    DEVICE_CONTEXT *context = raw;
    UNREFERENCED_PARAMETER(target);
    WdfSpinLockAcquire(Global.Lock);
    context->Connected = NT_SUCCESS(parameters->IoStatus.Status);
    update_state();
    WdfSpinLockRelease(Global.Lock);
    WdfRequestCompleteWithInformation(request, parameters->IoStatus.Status, parameters->IoStatus.Information);
}

/* CONNECT_DATA uses a PVOID for its callback by Windows API definition. */
#pragma warning(push)
#pragma warning(disable:4055 4152)
VOID PostureInternal(WDFQUEUE queue, WDFREQUEST request, size_t output,
                     size_t input, ULONG code) {
    WDFDEVICE device = WdfIoQueueGetDevice(queue);
    DEVICE_CONTEXT *context = DeviceContext(device);
    ULONG wanted = context->Role == ROLE_KEYBOARD ? IOCTL_INTERNAL_KEYBOARD_CONNECT : IOCTL_INTERNAL_MOUSE_CONNECT;
    NTSTATUS status;
    UNREFERENCED_PARAMETER(output);
    UNREFERENCED_PARAMETER(input);
    if (code == wanted) {
        PCONNECT_DATA connect;
        status = WdfRequestRetrieveInputBuffer(request, sizeof *connect, (PVOID *)&connect, NULL);
        if (!NT_SUCCESS(status)) { WdfRequestComplete(request, status); return; }
        WdfSpinLockAcquire(Global.Lock);
        if (context->Upper.ClassService != NULL || connect->ClassService == NULL || connect->ClassDeviceObject == NULL) {
            WdfSpinLockRelease(Global.Lock);
            WdfRequestComplete(request, STATUS_SHARING_VIOLATION);
            return;
        }
        context->Upper = *connect;
        connect->ClassDeviceObject = WdfDeviceWdmGetDeviceObject(device);
        connect->ClassService = context->Role == ROLE_KEYBOARD ? (PVOID)keyboard_service : (PVOID)mouse_service;
        WdfSpinLockRelease(Global.Lock);
        WdfRequestFormatRequestUsingCurrentType(request);
        WdfRequestSetCompletionRoutine(request, PostureConnected, context);
        if (!WdfRequestSend(request, WdfDeviceGetIoTarget(device), NULL))
            WdfRequestComplete(request, WdfRequestGetStatus(request));
        return;
    }
    WdfRequestFormatRequestUsingCurrentType(request);
    {
        WDF_REQUEST_SEND_OPTIONS options;
        WDF_REQUEST_SEND_OPTIONS_INIT(&options, WDF_REQUEST_SEND_OPTION_SEND_AND_FORGET);
        if (!WdfRequestSend(request, WdfDeviceGetIoTarget(device), &options))
            WdfRequestComplete(request, WdfRequestGetStatus(request));
    }
}
#pragma warning(pop)

NTSTATUS PostureD0Entry(WDFDEVICE device, WDF_POWER_DEVICE_STATE previous) {
    DEVICE_CONTEXT *context = DeviceContext(device);
    UNREFERENCED_PARAMETER(previous);
    WdfSpinLockAcquire(Global.Lock);
    context->Powered = TRUE;
    update_state();
    WdfSpinLockRelease(Global.Lock);
    return STATUS_SUCCESS;
}
NTSTATUS PostureD0Exit(WDFDEVICE device, WDF_POWER_DEVICE_STATE next) {
    DEVICE_CONTEXT *context = DeviceContext(device);
    UNREFERENCED_PARAMETER(next);
    WdfSpinLockAcquire(Global.Lock);
    context->Powered = FALSE;
    update_state();
    WdfSpinLockRelease(Global.Lock);
    return STATUS_SUCCESS;
}
VOID PostureRemoved(WDFDEVICE device) {
    WdfSpinLockAcquire(Global.Lock);
    DeviceContext(device)->Powered = FALSE;
    DeviceContext(device)->Connected = FALSE;
    update_state();
    WdfSpinLockRelease(Global.Lock);
}
VOID PostureDeviceCleanup(WDFOBJECT object) {
    DEVICE_CONTEXT *context = DeviceContext((WDFDEVICE)object);
    WdfSpinLockAcquire(Global.Lock);
    if (context->Registered) {
        context->Registered = FALSE;
        if (Global.Devices[context->Role] == context) Global.Devices[context->Role] = NULL;
        --Global.Counts[context->Role];
        posture_release(&Global.Guard);
        update_state();
    }
    WdfSpinLockRelease(Global.Lock);
}

NTSTATUS PostureDeviceAdd(WDFDRIVER driver, PWDFDEVICE_INIT init) {
    PDEVICE_OBJECT pdo = WdfFdoInitWdmGetPhysicalDevice(init);
    WCHAR instance[512], class_name[64];
    GUID bus, device_class;
    ULONG required;
    DEVPROPTYPE type;
    UNICODE_STRING id, wanted, class_string;
    int role = -1;
    NTSTATUS status;
    WDFDEVICE device;
    WDF_OBJECT_ATTRIBUTES attributes;
    WDF_IO_QUEUE_CONFIG queue;
    WDF_PNPPOWER_EVENT_CALLBACKS power;
    DEVICE_CONTEXT *context;
    UNREFERENCED_PARAMETER(driver);
    if (!Global.Configured) return STATUS_SUCCESS;
    status = IoGetDevicePropertyData(pdo, &DEVPKEY_Device_InstanceId, LOCALE_NEUTRAL, 0,
                                     sizeof instance, instance, &required, &type);
    if (!NT_SUCCESS(status) || type != DEVPROP_TYPE_STRING || required < sizeof(WCHAR) ||
        required > sizeof instance || required % sizeof(WCHAR) || instance[required / sizeof(WCHAR) - 1] != L'\0')
        return STATUS_SUCCESS;
    RtlInitUnicodeString(&id, instance);
    WdfStringGetUnicodeString(Global.KeyboardId, &wanted);
    if (RtlEqualUnicodeString(&id, &wanted, TRUE)) role = ROLE_KEYBOARD;
    WdfStringGetUnicodeString(Global.TouchpadId, &wanted);
    if (RtlEqualUnicodeString(&id, &wanted, TRUE)) role = ROLE_TOUCHPAD;
    if (role < 0) return STATUS_SUCCESS;
    status = IoGetDeviceProperty(pdo, DevicePropertyBusTypeGuid, sizeof bus, &bus, &required);
    if (!NT_SUCCESS(status) || (!IsEqualGUID(&bus, &GUID_BUS_TYPE_INTERNAL) && !IsEqualGUID(&bus, &GUID_BUS_TYPE_I8042)))
        return STATUS_SUCCESS;
    status = IoGetDeviceProperty(pdo, DevicePropertyClassGuid, sizeof class_name, class_name, &required);
    if (!NT_SUCCESS(status) || required < sizeof(WCHAR) || required > sizeof class_name ||
        class_name[required / sizeof(WCHAR) - 1] != L'\0') return STATUS_SUCCESS;
    RtlInitUnicodeString(&class_string, class_name);
    status = RtlGUIDFromString(&class_string, &device_class);
    if (!NT_SUCCESS(status) || !IsEqualGUID(&device_class,
        role == ROLE_KEYBOARD ? &GUID_DEVCLASS_KEYBOARD : &GUID_DEVCLASS_MOUSE)) return STATUS_SUCCESS;
    WdfFdoInitSetFilter(init);
    WDF_PNPPOWER_EVENT_CALLBACKS_INIT(&power);
    power.EvtDeviceD0Entry = PostureD0Entry;
    power.EvtDeviceD0Exit = PostureD0Exit;
    power.EvtDeviceSurpriseRemoval = PostureRemoved;
    WdfDeviceInitSetPnpPowerEventCallbacks(init, &power);
    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, DEVICE_CONTEXT);
    attributes.EvtCleanupCallback = PostureDeviceCleanup;
    status = WdfDeviceCreate(&init, &attributes, &device);
    if (!NT_SUCCESS(status)) return status;
    context = DeviceContext(device);
    context->Role = role;
    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queue, WdfIoQueueDispatchParallel);
    queue.EvtIoInternalDeviceControl = PostureInternal;
    status = WdfIoQueueCreate(device, &queue, WDF_NO_OBJECT_ATTRIBUTES, WDF_NO_HANDLE);
    if (!NT_SUCCESS(status)) return status;
    WdfSpinLockAcquire(Global.Lock);
    if (Global.Counts[role]++ == 0) Global.Devices[role] = context;
    else Global.Devices[role] = NULL;
    context->Registered = TRUE;
    update_state();
    WdfSpinLockRelease(Global.Lock);
    return STATUS_SUCCESS;
}

VOID PostureFileCreate(WDFDEVICE device, WDFREQUEST request, WDFFILEOBJECT file) {
    UNREFERENCED_PARAMETER(device);
    RtlZeroMemory(FileContext(file), sizeof(FILE_CONTEXT));
    WdfRequestComplete(request, STATUS_SUCCESS);
}
VOID PostureFileCleanup(WDFFILEOBJECT file) {
    UNREFERENCED_PARAMETER(file);
    WdfSpinLockAcquire(Global.Lock);
    posture_release(&Global.Guard);
    WdfSpinLockRelease(Global.Lock);
}
VOID PostureWatchdog(WDFTIMER timer) {
    UNREFERENCED_PARAMETER(timer);
    WdfSpinLockAcquire(Global.Lock);
    (void)posture_blocked(&Global.Guard, now_ms());
    WdfSpinLockRelease(Global.Lock);
}
VOID PostureControl(WDFQUEUE queue, WDFREQUEST request, size_t output, size_t input, ULONG code) {
    struct posture_request local, *source;
    struct posture_reply *reply;
    NTSTATUS status = STATUS_INVALID_DEVICE_REQUEST;
    UNREFERENCED_PARAMETER(queue);
    if (code != IOCTL_POSTURE_CONTROL || input != sizeof local || output < sizeof *reply) goto invalid;
    status = WdfRequestRetrieveInputBuffer(request, sizeof local, (PVOID *)&source, NULL);
    if (!NT_SUCCESS(status)) goto invalid;
    local = *source;
    status = WdfRequestRetrieveOutputBuffer(request, sizeof *reply, (PVOID *)&reply, NULL);
    if (!NT_SUCCESS(status)) goto invalid;
    WdfSpinLockAcquire(Global.Lock);
    update_state();
    posture_command(&Global.Guard, &FileContext(WdfRequestGetFileObject(request))->Connection,
                    &local, now_ms(), reply);
    WdfSpinLockRelease(Global.Lock);
    WdfRequestCompleteWithInformation(request, STATUS_SUCCESS, sizeof *reply);
    return;
invalid:
    WdfSpinLockAcquire(Global.Lock);
    posture_release(&Global.Guard);
    WdfSpinLockRelease(Global.Lock);
    WdfRequestComplete(request, status);
}

static void load_configuration(WDFDRIVER driver) {
    WDFKEY key;
    WDF_OBJECT_ATTRIBUTES attributes;
    UNICODE_STRING keyboard = RTL_CONSTANT_STRING(L"KeyboardInstanceId");
    UNICODE_STRING touchpad = RTL_CONSTANT_STRING(L"TouchpadInstanceId");
    UNICODE_STRING left, right;
    WDF_OBJECT_ATTRIBUTES_INIT(&attributes);
    attributes.ParentObject = driver;
    if (!NT_SUCCESS(WdfStringCreate(NULL, &attributes, &Global.KeyboardId)) ||
        !NT_SUCCESS(WdfStringCreate(NULL, &attributes, &Global.TouchpadId))) return;
    if (!NT_SUCCESS(WdfDriverOpenParametersRegistryKey(driver, KEY_READ, WDF_NO_OBJECT_ATTRIBUTES, &key))) return;
    if (NT_SUCCESS(WdfRegistryQueryString(key, &keyboard, Global.KeyboardId)) &&
        NT_SUCCESS(WdfRegistryQueryString(key, &touchpad, Global.TouchpadId))) {
        WdfStringGetUnicodeString(Global.KeyboardId, &left);
        WdfStringGetUnicodeString(Global.TouchpadId, &right);
        Global.Configured = left.Length > 0 && right.Length > 0 && left.Length < 1024 && right.Length < 1024 &&
                            !RtlEqualUnicodeString(&left, &right, TRUE);
    }
    WdfRegistryClose(key);
}

VOID PostureUnload(WDFDRIVER driver) {
    UNREFERENCED_PARAMETER(driver);
    if (Global.Timer != NULL) WdfTimerStop(Global.Timer, TRUE);
    if (Global.Lock != NULL) {
        WdfSpinLockAcquire(Global.Lock);
        posture_release(&Global.Guard);
        WdfSpinLockRelease(Global.Lock);
    }
    if (Global.Control != NULL) WdfObjectDelete(Global.Control);
}

NTSTATUS DriverEntry(PDRIVER_OBJECT object, PUNICODE_STRING registry) {
    WDF_DRIVER_CONFIG config;
    WDFDRIVER driver;
    PWDFDEVICE_INIT init;
    WDF_OBJECT_ATTRIBUTES attributes, file_attributes;
    WDF_FILEOBJECT_CONFIG files;
    WDF_IO_QUEUE_CONFIG queue;
    WDF_TIMER_CONFIG timer;
    UNICODE_STRING name = RTL_CONSTANT_STRING(L"\\Device\\PanasonicPostureControl");
    UNICODE_STRING sddl = RTL_CONSTANT_STRING(L"O:SYG:SYD:P(A;;GA;;;SY)");
    NTSTATUS status;
    RtlZeroMemory(&Global, sizeof Global);
    WDF_DRIVER_CONFIG_INIT(&config, PostureDeviceAdd);
    config.EvtDriverUnload = PostureUnload;
    status = WdfDriverCreate(object, registry, WDF_NO_OBJECT_ATTRIBUTES, &config, &driver);
    if (!NT_SUCCESS(status)) return status;
    load_configuration(driver);
    init = WdfControlDeviceInitAllocate(driver, &sddl);
    if (init == NULL) return STATUS_INSUFFICIENT_RESOURCES;
    WdfDeviceInitSetExclusive(init, TRUE);
    status = WdfDeviceInitAssignName(init, &name);
    if (!NT_SUCCESS(status)) { WdfDeviceInitFree(init); return status; }
    WDF_FILEOBJECT_CONFIG_INIT(&files, PostureFileCreate, WDF_NO_EVENT_CALLBACK, PostureFileCleanup);
    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&file_attributes, FILE_CONTEXT);
    WdfDeviceInitSetFileObjectConfig(init, &files, &file_attributes);
    status = WdfDeviceCreate(&init, WDF_NO_OBJECT_ATTRIBUTES, &Global.Control);
    if (!NT_SUCCESS(status)) { if (init != NULL) WdfDeviceInitFree(init); return status; }
    WDF_OBJECT_ATTRIBUTES_INIT(&attributes);
    attributes.ParentObject = Global.Control;
    status = WdfSpinLockCreate(&attributes, &Global.Lock);
    if (!NT_SUCCESS(status)) return status;
    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queue, WdfIoQueueDispatchParallel);
    queue.PowerManaged = WdfFalse;
    queue.EvtIoDeviceControl = PostureControl;
    status = WdfIoQueueCreate(Global.Control, &queue, WDF_NO_OBJECT_ATTRIBUTES, WDF_NO_HANDLE);
    if (!NT_SUCCESS(status)) return status;
    WDF_TIMER_CONFIG_INIT_PERIODIC(&timer, PostureWatchdog, 100);
    timer.AutomaticSerialization = FALSE;
    status = WdfTimerCreate(&timer, &attributes, &Global.Timer);
    if (!NT_SUCCESS(status)) return status;
    WdfTimerStart(Global.Timer, WDF_REL_TIMEOUT_IN_MS(100));
    WdfControlFinishInitializing(Global.Control);
    return STATUS_SUCCESS;
}
