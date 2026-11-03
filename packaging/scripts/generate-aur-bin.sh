#!/usr/bin/env bash
# Generate AUR metadata for the prebuilt Arch package from a verified release
# archive. The caller must run makepkg --printsrcinfo for the final metadata.
set -euo pipefail
export LC_ALL=C

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)

if [[ $# -ne 3 ]]; then
  echo "usage: $0 TAG PACKAGE OUTPUT_DIR" >&2
  exit 2
fi

tag=$1
package=$2
output_dir=$3

version=$(python3 "$repo_root/packaging/scripts/release_version.py" --tag "$tag")
expected="panasonic-posture-manager-${version}-1-x86_64.pkg.tar.zst"
archive=$(basename -- "$package")

if [[ ! -f $package ]]; then
  echo "error: Arch package not found: $package" >&2
  exit 1
fi
if [[ $archive != "$expected" ]]; then
  echo "error: expected Arch package $expected, got $archive" >&2
  exit 1
fi

sha256=$(sha256sum -- "$package" | cut -d' ' -f1)
if [[ ! $sha256 =~ ^[0-9a-f]{64}$ ]]; then
  echo "error: cannot calculate the Arch package checksum" >&2
  exit 1
fi

mkdir -p "$output_dir"
sed \
  -e "s/@VERSION@/${version}/g" \
  -e "s/@SHA256@/${sha256}/g" \
  "$repo_root/packaging/aur-bin/PKGBUILD.in" >"$output_dir/PKGBUILD"
install -m644 \
  "$repo_root/packaging/aur/panasonic-posture-manager.install" \
  "$output_dir/panasonic-posture-manager-bin.install"

if grep -q '@[A-Z][A-Z0-9_]*@' "$output_dir/PKGBUILD"; then
  echo "error: generated PKGBUILD still contains template placeholders" >&2
  exit 1
fi

printf '%s\n' "$output_dir/PKGBUILD"
