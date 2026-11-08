"""Compile the development KMDF filter using explicitly supplied local tools.

Produces an UNSIGNED image for inspection, not an installable driver package.
Does not download tools, install services, enroll devices or change Windows
boot/security settings. SDK/WDK headers and WDF libraries must already exist.
"""
import argparse
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def build(compiler: Path, sdk: Path, wdk: Path, output: Path, *, control_fixture=False) -> Path:
    compiler, sdk, wdk, output = [path.resolve() for path in (compiler, sdk, wdk, output)]
    includes = [wdk / 'c/Include/10.0.26100.0/km', wdk / 'c/Include/10.0.26100.0/km/crt',
                wdk / 'c/Include/wdf/kmdf/1.31', sdk / 'c/Include/10.0.26100.0/shared',
                sdk / 'c/Include/10.0.26100.0/um', sdk / 'c/Include/10.0.26100.0/ucrt']
    kernel = wdk / 'c/Lib/10.0.26100.0/km/x64'
    wdf = wdk / 'c/Lib/wdf/kmdf/x64/1.31'
    libraries = [kernel / name for name in ('ntoskrnl.lib', 'hal.lib', 'wmilib.lib', 'BufferOverflowK.lib')]
    libraries += [wdf / name for name in ('wdfdriverentry.lib', 'wdfldr.lib')]
    resource = compiler.parent / 'lib'
    required = [compiler, resource / 'include/stdint.h', *libraries, *includes]
    if any(not path.exists() for path in required):
        raise ValueError('compiler or matching SDK/WDK 26100 and KMDF 1.31 files are missing')
    output.mkdir(parents=True, exist_ok=True)
    common = [str(compiler), 'clang', '-target', 'x86_64-pc-windows-msvc', '-std=c11',
              '-Wall', '-Wextra', '-Werror', '-fms-extensions', '-fms-compatibility',
              '-fms-compatibility-version=19.40', '-fstack-protector-strong',
              '-resource-dir', str(resource), '-D_AMD64_', '-D_KERNEL_MODE',
              '-DNTDDI_VERSION=0x0A000008', '-D_WIN32_WINNT=0x0A00']
    for include in includes:
        common += ['-isystem', str(include)]
    objects = []
    for name in ('driver', 'control'):
        obj = output / (name + '.obj')
        subprocess.run(common + ['-c', str(ROOT / 'backend/windows' / (name + '.c')),
                                 '-o', str(obj)], check=True)
        objects.append(obj)
    image = output / 'posture-filter-unsigned.sys'
    subprocess.run([str(compiler), 'lld-link', '/driver', '/subsystem:native', '/entry:FxDriverEntry',
        '/machine:x64', '/nodefaultlib', '/nxcompat', '/dynamicbase', '/opt:ref', '/opt:icf',
        '/out:' + str(image), *map(str, objects), *map(str, libraries)], check=True)
    # Do not call a successful linker run a driver build without inspecting its
    # PE identity. This still says nothing about kernel load or device safety.
    data = image.read_bytes()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if (data[:2] != b'MZ' or data[pe:pe+4] != b'PE\0\0' or
            struct.unpack_from('<H', data, pe+4)[0] != 0x8664 or
            struct.unpack_from('<H', data, pe+24)[0] != 0x20b or
            struct.unpack_from('<H', data, pe+24+68)[0] != 1):
        raise ValueError('linker output is not an AMD64 native PE32+ image')
    if control_fixture:
        # An unprivileged DLL of pure control.c only: no WDF, driver entry,
        # handles, device installation or input access. Exercises the Python
        # actuator against the actual C ABI and watchdog decisions.
        subprocess.run([str(compiler), 'lld-link', '/dll', '/noentry', '/machine:x64',
            '/nodefaultlib', '/nxcompat', '/dynamicbase', '/export:posture_release',
            '/export:posture_blocked', '/export:posture_command',
            '/out:' + str(output / 'posture-control-fixture.dll'), str(objects[1])], check=True)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', type=Path, required=True, help='local Zig executable with clang/lld-link')
    parser.add_argument('--sdk', type=Path, required=True, help='extracted Microsoft.Windows.SDK.CPP package')
    parser.add_argument('--wdk', type=Path, required=True, help='extracted Microsoft.Windows.WDK.x64 package')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/windows-driver')
    parser.add_argument('--control-fixture', action='store_true', help='also build a pure C user-mode DLL for ABI tests')
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run this explicit development build on Windows')
    print(build(options.compiler, options.sdk, options.wdk, options.output, control_fixture=options.control_fixture))


if __name__ == '__main__':
    main()
