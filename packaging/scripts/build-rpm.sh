#!/usr/bin/env bash
# Build the Fedora/RHEL-family binary RPM.
#
# Run this on an x86_64 RPM system with gcc, gettext, make, rpm-build,
# python3-devel, python3-build, python3-installer, python3-setuptools and
# python3-wheel installed.  The GitHub release workflow runs it in Fedora.
set -euo pipefail
export LC_ALL=C

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
output_dir=${1:-"$repo_root/dist/packages"}

version=$(python3 "$repo_root/packaging/scripts/release_version.py")
arch=$(uname -m)
if [[ $arch != x86_64 ]]; then
  echo "error: this project currently publishes only x86_64 RPMs, not $arch" >&2
  exit 1
fi

# The source archive is made from HEAD.  Refuse to mix uncommitted metadata
# with a package whose version and installed files come from that commit.
dirty=$(git -C "$repo_root" status --porcelain --untracked-files=normal)
if [[ -n $dirty ]]; then
  echo "error: commit or stash working-tree changes before building an RPM:" >&2
  printf '%s\n' "$dirty" >&2
  exit 1
fi

source_name=panasonic-posture-manager-$version
commit_epoch=$(git -C "$repo_root" -c safe.directory="$repo_root" \
  show -s --format=%ct HEAD)
changelog_date=$(date -u --date="@$commit_epoch" '+%a %b %d %Y')

build_root=$(mktemp -d "${TMPDIR:-/tmp}/ppm-rpm.XXXXXX")
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT INT TERM

mkdir -p \
  "$build_root/BUILD" \
  "$build_root/BUILDROOT" \
  "$build_root/RPMS" \
  "$build_root/SOURCES" \
  "$build_root/SPECS" \
  "$build_root/SRPMS"

git -C "$repo_root" -c safe.directory="$repo_root" archive \
  --format=tar.gz \
  --prefix="$source_name/" \
  HEAD >"$build_root/SOURCES/$source_name.tar.gz"

sed \
  -e "s/@VERSION@/$version/g" \
  -e "s/@DATE@/$changelog_date/g" \
  "$repo_root/packaging/rpm/panasonic-posture-manager.spec.in" \
  >"$build_root/SPECS/panasonic-posture-manager.spec"

rpmbuild -bb \
  --define "_topdir $build_root" \
  --define "_build_id_links none" \
  "$build_root/SPECS/panasonic-posture-manager.spec"

mapfile -t packages < <(
  find "$build_root/RPMS" -type f \
    -name "panasonic-posture-manager-${version}-1.*.rpm" -print
)
if [[ ${#packages[@]} -ne 1 ]]; then
  echo "error: expected exactly one binary RPM, found ${#packages[@]}" >&2
  exit 1
fi

mkdir -p "$output_dir"
output_dir=$(CDPATH='' cd -- "$output_dir" && pwd)
package=$output_dir/$(basename "${packages[0]}")
install -Dm644 "${packages[0]}" "$package"
rpm -qip "$package" >/dev/null
rpm -qlp "$package" >/dev/null
printf '%s\n' "$package"
