#!/usr/bin/env bash
# Populate a DESTDIR with the files shared by the deb and RPM packages.
#
# Usage: stage-install-tree.sh DESTDIR WHEEL
#
# The caller is responsible for building backend/relay, the gettext catalog
# and the wheel first.  The wheel installer owns the Python site-packages
# path, so Debian's dist-packages and Fedora's site-packages layouts are both
# handled without hard-coding a Python version.
set -euo pipefail
export LC_ALL=C

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)

if [[ $# -ne 2 ]]; then
  echo "usage: $0 DESTDIR WHEEL" >&2
  exit 2
fi

destdir=${1%/}
wheel=$2
if [[ -z $destdir ]]; then
  echo "error: DESTDIR must not be empty" >&2
  exit 2
fi
if [[ $destdir != /* ]]; then
  destdir=$PWD/$destdir
fi
if [[ ! -f $wheel ]]; then
  echo "error: wheel not found: $wheel" >&2
  exit 1
fi
if [[ ! -x $repo_root/backend/relay ]]; then
  echo "error: backend/relay has not been built" >&2
  exit 1
fi
if [[ ! -f $repo_root/locale/zh_CN/LC_MESSAGES/panasonic-posture-manager.mo ]]; then
  echo "error: the gettext catalog has not been built" >&2
  exit 1
fi

mkdir -p "$destdir"

# Install the wheel without generating bytecode.  Distribution packages own
# their file lists, and unversioned .pyc files make upgrades unnecessarily
# fragile when the system Python is upgraded.
if python3 -c 'import sysconfig; raise SystemExit(0 if "deb_system" in sysconfig.get_scheme_names() else 1)'; then
  # Debian and Ubuntu default to /usr/local for ``pip install``.  Their Python
  # patch adds the deb_system scheme for distribution packages, which resolves
  # purelib to /usr/lib/python3/dist-packages under the /usr prefix.
  export DEB_PYTHON_INSTALL_LAYOUT=deb_system
fi
python3 -m installer \
  --destdir "$destdir" \
  --prefix /usr \
  --no-compile-bytecode \
  "$wheel"

# Some Python installers retain the interpreter used for the build in the
# entry-point shebang.  Every supported distribution provides python3, while
# /usr/bin/python is not guaranteed outside Arch Linux.
while IFS= read -r -d '' script; do
  if head -n 1 "$script" | grep -Eq '^#!.*python'; then
    sed -i '1s|.*|#!/usr/bin/python3|' "$script"
  fi
done < <(find "$destdir/usr/bin" -maxdepth 1 -type f -print0 2>/dev/null)

datadir=$destdir/usr/share/panasonic-posture-manager
while IFS= read -r -d '' file; do
  install -Dm644 "$file" "$datadir/${file#"$repo_root/data/"}"
done < <(find "$repo_root/data" -type f -print0)

install -Dm644 \
  "$repo_root/data/assets/work.mmoe.panasonic.PostureManager.png" \
  "$destdir/usr/share/icons/hicolor/512x512/apps/work.mmoe.panasonic.PostureManager.png"

install -Dm755 "$repo_root/backend/relay" \
  "$destdir/usr/lib/panasonic-posture-manager/relay"

while IFS= read -r -d '' file; do
  install -Dm644 "$file" \
    "$destdir/usr/share/locale/${file#"$repo_root/locale/"}"
done < <(find "$repo_root/locale" -type f -name '*.mo' -print0)

install -Dm644 "$repo_root/packaging/systemd/panasonic-posture-manager.service" \
  "$destdir/usr/lib/systemd/system/panasonic-posture-manager.service"
install -Dm644 \
  "$repo_root/packaging/systemd/user/panasonic-posture-manager-session.service" \
  "$destdir/usr/lib/systemd/user/panasonic-posture-manager-session.service"
install -Dm644 "$repo_root/packaging/modules-load.d/uinput.conf" \
  "$destdir/usr/lib/modules-load.d/panasonic-posture-manager.conf"
install -Dm644 "$repo_root/packaging/dbus/work.mmoe.panasonic.Posture.conf" \
  "$destdir/usr/share/dbus-1/system.d/work.mmoe.panasonic.Posture.conf"
install -Dm644 "$repo_root/packaging/dbus/work.mmoe.panasonic.Posture.service" \
  "$destdir/usr/share/dbus-1/system-services/work.mmoe.panasonic.Posture.service"
install -Dm644 "$repo_root/packaging/polkit/work.mmoe.panasonic.Posture.policy" \
  "$destdir/usr/share/polkit-1/actions/work.mmoe.panasonic.Posture.policy"
install -Dm644 \
  "$repo_root/packaging/desktop/work.mmoe.panasonic.PostureManager.desktop" \
  "$destdir/usr/share/applications/work.mmoe.panasonic.PostureManager.desktop"
install -Dm644 "$repo_root/LICENSE" \
  "$destdir/usr/share/licenses/panasonic-posture-manager/LICENSE"

for required in \
  usr/bin/panasonic-posture-manager \
  usr/bin/panasonic-posture-manager-probe \
  usr/lib/panasonic-posture-manager/relay \
  usr/share/licenses/panasonic-posture-manager/LICENSE \
  usr/share/panasonic-posture-manager/assets/laptop.svg \
  usr/share/locale/zh_CN/LC_MESSAGES/panasonic-posture-manager.mo; do
  if [[ ! -e $destdir/$required ]]; then
    echo "error: staged tree is missing $required" >&2
    exit 1
  fi
done

if find "$destdir" -name '*.pyc' -print -quit | grep -q .; then
  echo "error: staged tree contains Python bytecode" >&2
  exit 1
fi
if grep -rIq '/usr/local' "$destdir"; then
  echo "error: staged tree references /usr/local" >&2
  exit 1
fi
