#!/usr/bin/env bash
# Publish the already-validated AUR metadata for a release tag.
#
# Required environment:
#   AUR_SSH_PRIVATE_KEY  private SSH key registered with the AUR account
#   RELEASE_TAG          release tag such as v2.0.0
#
# Optional environment:
#   AUR_GIT_NAME         commit author name  (default: adogecheems)
#   AUR_GIT_EMAIL        commit author email (default: project maintainer)
#   AUR_PACKAGE          AUR package name    (default: panasonic-posture-manager)
#
# Usage: publish-aur.sh [METADATA_DIR]
#
# METADATA_DIR must contain PKGBUILD, the install script and either SRCINFO or
# .SRCINFO.  The release workflow generates SRCINFO in the Arch build job so a
# runner without makepkg can still publish it.
set -euo pipefail
export LC_ALL=C

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
metadata_dir=${1:-"$repo_root/packaging/aur"}
metadata_dir=$(CDPATH='' cd -- "$metadata_dir" && pwd)
package=${AUR_PACKAGE:-panasonic-posture-manager}
remote=${AUR_REMOTE:-ssh://aur@aur.archlinux.org/${package}.git}

if [[ -z ${AUR_SSH_PRIVATE_KEY:-} ]]; then
  echo "error: AUR_SSH_PRIVATE_KEY is not configured" >&2
  exit 1
fi
if [[ -z ${RELEASE_TAG:-} ]]; then
  echo "error: RELEASE_TAG is not configured" >&2
  exit 1
fi

version=$(python3 "$repo_root/packaging/scripts/release_version.py" --tag "$RELEASE_TAG")

if [[ -f $metadata_dir/.SRCINFO ]]; then
  srcinfo=$metadata_dir/.SRCINFO
elif [[ -f $metadata_dir/SRCINFO ]]; then
  srcinfo=$metadata_dir/SRCINFO
else
  echo "error: no .SRCINFO or SRCINFO in $metadata_dir" >&2
  exit 1
fi
for required in \
  "$metadata_dir/PKGBUILD" \
  "$metadata_dir/$package.install" \
  "$srcinfo"; do
  if [[ ! -f $required ]]; then
    echo "error: AUR metadata is missing $required" >&2
    exit 1
  fi
done
if ! grep -q "^[[:space:]]*pkgver = ${version}$" "$srcinfo"; then
  echo "error: AUR metadata does not describe version $version" >&2
  exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/ppm-aur.XXXXXX")
ssh_dir=$(mktemp -d "${TMPDIR:-/tmp}/ppm-aur-ssh.XXXXXX")
cleanup() {
  rm -rf -- "$work" "$ssh_dir"
}
trap cleanup EXIT INT TERM

key=$ssh_dir/id_ed25519
printf '%s\n' "$AUR_SSH_PRIVATE_KEY" >"$key"
chmod 600 "$key"
if ! ssh-keygen -y -f "$key" >/dev/null 2>&1; then
  echo "error: AUR_SSH_PRIVATE_KEY is not a readable private key" >&2
  exit 1
fi

known_hosts=$repo_root/packaging/ci/aur_known_hosts
if [[ ! -f $known_hosts ]]; then
  echo "error: pinned AUR SSH host key not found: $known_hosts" >&2
  exit 1
fi

export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts"

git clone --depth 1 "$remote" "$work/aur"
install -m644 "$metadata_dir/PKGBUILD" "$work/aur/PKGBUILD"
install -m644 "$metadata_dir/$package.install" "$work/aur/$package.install"
install -m644 "$srcinfo" "$work/aur/.SRCINFO"

cd "$work/aur"
git config user.name "${AUR_GIT_NAME:-adogecheems}"
git config user.email "${AUR_GIT_EMAIL:-adogecheems@outlook.com}"
git add -- PKGBUILD .SRCINFO "$package.install"
if git diff --cached --quiet; then
  echo "AUR package $package is already at $version"
  exit 0
fi
git diff --cached --check
git commit -m "Update to $version"
git push origin HEAD
echo "Published $package $version to the AUR"
