param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 65535)]
    [int]$LanguageId,

    [Parameter(Mandatory = $true)]
    [string]$InstallFolder
)

$ErrorActionPreference = 'Stop'
$InstallFolder = [IO.Path]::GetFullPath(
    $InstallFolder.Trim('"').TrimEnd([char[]]@('\', '/'))
)
$RuntimeFolder = Join-Path (Split-Path -Parent $PSCommandPath) 'runtime'
$simplifiedChinese = $LanguageId -in @(0x0004, 0x0804, 0x1004)
$displayName = if ($simplifiedChinese) {
    '松下二合一平板电脑姿态管理工具'
} else {
    'Panasonic 2-in-1 Tablet Posture Management Tool'
}
$shortcutDescription = if ($simplifiedChinese) {
    "打开 $displayName"
} else {
    "Open $displayName"
}
$knownNames = @(
    'Panasonic 2-in-1 Tablet Posture Management Tool',
    '松下二合一平板电脑姿态管理工具',
    'Panasonic Posture Manager'
)
$programs = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
$destinationLink = Join-Path $programs "$displayName.lnk"

foreach ($name in $knownNames) {
    $candidateLink = Join-Path $programs "$name.lnk"
    if ($candidateLink -ne $destinationLink -and
            (Test-Path -LiteralPath $candidateLink -PathType Leaf)) {
        Remove-Item -LiteralPath $candidateLink -Force
    }
    $folder = Join-Path $programs $name
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        continue
    }
    foreach ($candidateName in $knownNames) {
        $candidate = Join-Path $folder "$candidateName.lnk"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Remove-Item -LiteralPath $candidate -Force
        }
    }
    Remove-Item -LiteralPath $folder -Force -ErrorAction SilentlyContinue
}

$target = Join-Path $RuntimeFolder 'pythonw.exe'
$launcher = Join-Path $InstallFolder 'launch.py'
$icon = Join-Path $InstallFolder 'data\assets\icons\work.mmoe.panasonic.PostureManager.ico'
$appUserModelId = 'work.mmoe.panasonic.PostureManager'
if (Test-Path -LiteralPath $destinationLink -PathType Leaf) {
    Remove-Item -LiteralPath $destinationLink -Force
}
# WScript.Shell saves through the system code page and loses characters on
# English Windows. Use IShellLinkW and IPersistFile for Unicode paths, and keep
# System.AppUserModel.ID on the same object so notifications retain our identity.
if (-not ('PanasonicPosture.Windows.ShortcutIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace PanasonicPosture.Windows
{
    public static class ShortcutIdentity
    {
        [ComImport]
        [Guid("000214F9-0000-0000-C000-000000000046")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellLinkW
        {
            [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr findData, uint flags);
            [PreserveSig] int GetIDList(out IntPtr idList);
            [PreserveSig] int SetIDList(IntPtr idList);
            [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description, int size);
            [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
            [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory, int size);
            [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
            [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments, int size);
            [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
            [PreserveSig] int GetHotkey(out short hotkey);
            [PreserveSig] int SetHotkey(short hotkey);
            [PreserveSig] int GetShowCmd(out int showCommand);
            [PreserveSig] int SetShowCmd(int showCommand);
            [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder icon, int size, out int index);
            [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string icon, int index);
            [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
            [PreserveSig] int Resolve(IntPtr window, uint flags);
            [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
        }

        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        private struct PropertyKey
        {
            public Guid FormatId;
            public int PropertyId;

            public PropertyKey(Guid formatId, int propertyId)
            {
                FormatId = formatId;
                PropertyId = propertyId;
            }
        }

        [StructLayout(LayoutKind.Explicit, Size = 24)]
        private struct PropVariant
        {
            [FieldOffset(0)] public ushort VariantType;
            [FieldOffset(8)] public IntPtr PointerValue;
        }

        [ComImport]
        [Guid("0000010b-0000-0000-C000-000000000046")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPersistFile
        {
            [PreserveSig] int GetClassID(out Guid classId);
            [PreserveSig] int IsDirty();
            [PreserveSig] int Load(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName,
                int mode);
            [PreserveSig] int Save(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName,
                [MarshalAs(UnmanagedType.Bool)] bool remember);
            [PreserveSig] int SaveCompleted(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName);
            [PreserveSig] int GetCurFile(out IntPtr fileName);
        }

        [ComImport]
        [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPropertyStore
        {
            [PreserveSig] int GetCount(out uint propertyCount);
            [PreserveSig] int GetAt(uint propertyIndex, out PropertyKey key);
            [PreserveSig] int GetValue(
                ref PropertyKey key,
                out PropVariant value);
            [PreserveSig] int SetValue(
                ref PropertyKey key,
                ref PropVariant value);
            [PreserveSig] int Commit();
        }

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PropVariant value);

        private static readonly Guid ShellLinkClassId =
            new Guid("00021401-0000-0000-C000-000000000046");
        private static readonly PropertyKey AppUserModelIdKey =
            new PropertyKey(
                new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
                5);

        private static void Check(int result)
        {
            if (result < 0)
            {
                Marshal.ThrowExceptionForHR(result);
            }
        }

        public static void Create(string shortcutPath, string target,
            string arguments, string workingDirectory, string icon,
            string description, string appUserModelId)
        {
            object link = Activator.CreateInstance(
                Type.GetTypeFromCLSID(ShellLinkClassId));
            try
            {
                var shellLink = (IShellLinkW)link;
                Check(shellLink.SetPath(target));
                Check(shellLink.SetArguments(arguments));
                Check(shellLink.SetWorkingDirectory(workingDirectory));
                Check(shellLink.SetIconLocation(icon, 0));
                Check(shellLink.SetDescription(description));
                var store = (IPropertyStore)link;
                var value = new PropVariant();
                value.VariantType = 31; // VT_LPWSTR
                value.PointerValue =
                    Marshal.StringToCoTaskMemUni(appUserModelId);
                var key = AppUserModelIdKey;
                try
                {
                    Check(store.SetValue(ref key, ref value));
                }
                finally
                {
                    PropVariantClear(ref value);
                }
                Check(store.Commit());
                Check(((IPersistFile)link).Save(shortcutPath, true));
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }

        public static string Get(string shortcutPath)
        {
            object link = Activator.CreateInstance(
                Type.GetTypeFromCLSID(ShellLinkClassId));
            try
            {
                Check(((IPersistFile)link).Load(shortcutPath, 0));
                var store = (IPropertyStore)link;
                var key = AppUserModelIdKey;
                PropVariant value;
                Check(store.GetValue(ref key, out value));
                try
                {
                    return value.VariantType == 31
                        ? Marshal.PtrToStringUni(value.PointerValue)
                        : null;
                }
                finally
                {
                    PropVariantClear(ref value);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }
    }
}
'@
}

[IO.Directory]::CreateDirectory($programs) | Out-Null
[PanasonicPosture.Windows.ShortcutIdentity]::Create(
    $destinationLink,
    $target,
    "-B -I `"$launcher`"",
    $InstallFolder,
    $icon,
    $shortcutDescription,
    $appUserModelId
)
$actualAppUserModelId = [PanasonicPosture.Windows.ShortcutIdentity]::Get(
    $destinationLink
)
if ($actualAppUserModelId -ne $appUserModelId) {
    throw "Start menu shortcut AppUserModelID verification failed"
}
