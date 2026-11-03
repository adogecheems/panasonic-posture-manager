#!/usr/bin/env bash
# Build the Debian/Ubuntu binary package.
#
# Run this on an amd64 Debian-family system with gcc, gettext, make,
# python3-build, python3-installer, python3-setuptools, python3-wheel and
# dpkg-dev installed.  The GitHub release workflow runs it in Ubuntu 24.04.
set -euo pipefail
export LC_ALL=C

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
output_dir=${1:-"$repo_root/dist/packages"}

version=$(python3 "$repo_root/packaging/scripts/release_version.py")
arch=$(dpkg --print-architecture)
if [[ $arch != amd64 ]]; then
  echo "error: this project currently publishes only amd64 packages, not $arch" >&2
  exit 1
fi

build_root=$(mktemp -d "${TMPDIR:-/tmp}/ppm-deb.XXXXXX")
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT INT TERM

(
  cd "$repo_root"
  make clean >/dev/null
  make WERROR=1
  build_args=(--wheel)
  if [[ ${PPM_BUILD_NO_ISOLATION:-0} == 1 ]]; then
    build_args+=(--no-isolation)
  fi
  python3 -m build "${build_args[@]}"
)

wheels=("$repo_root"/dist/panasonic_posture_manager-"$version"-*.whl)
if [[ ${#wheels[@]} -ne 1 || ! -f ${wheels[0]} ]]; then
  echo "error: expected exactly one wheel for $version in dist/" >&2
  exit 1
fi

stage=$build_root/root
bash "$repo_root/packaging/scripts/stage-install-tree.sh" "$stage" "${wheels[0]}"

docdir=$stage/usr/share/doc/panasonic-posture-manager
install -d "$docdir"
install -Dm644 "$repo_root/packaging/deb/copyright" "$docdir/copyright"

cat >"$build_root/changelog.Debian" <<EOF
panasonic-posture-manager (${version}-1) unstable; urgency=medium

  * Automated package build for upstream release ${version}.

 -- adogecheems <adogecheems@outlook.com>  $(date -R)
EOF
gzip -9n -c "$build_root/changelog.Debian" >"$docdir/changelog.Debian.gz"

mkdir -p "$stage/DEBIAN"
installed_size=$(du -sk "$stage" | cut -f1)
sed \
  -e "s/@VERSION@/${version}/g" \
  -e "s/@ARCH@/${arch}/g" \
  -e "s/@INSTALLED_SIZE@/${installed_size}/g" \
  "$repo_root/packaging/deb/control.in" >"$stage/DEBIAN/control"

for script in postinst prerm postrm; do
  install -Dm755 "$repo_root/packaging/deb/$script" "$stage/DEBIAN/$script"
done

(
  cd "$stage"
  find usr -type f -print0 | sort -z | xargs -0 -r md5sum >DEBIAN/md5sums
)

mkdir -p "$output_dir"
output_dir=$(CDPATH='' cd -- "$output_dir" && pwd)
package=$output_dir/panasonic-posture-manager_${version}-1_${arch}.deb
dpkg-deb --build --root-owner-group "$stage" "$package" >/dev/null
dpkg-deb --info "$package" >/dev/null
dpkg-deb --contents "$package" >/dev/null
printf '%s\n' "$package"
