#!/usr/bin/env bash
# Install the Linux desktop packages for real, in the distros they target, and
# assert what only a real install can show.
#
# WHY A SCRIPT AND NOT INLINE YAML: two callers need the identical assertions --
# ci.yml runs it on a packaging-path PR, build-desktop.yml runs it on every
# nightly and release. Inlining is how those two drift, and a drifted smoke test
# is worse than none: the lane that matters would be the one missing a check.
#
# Everything here is invisible to a unit test, because it lives in metadata the
# package manager interprets rather than in code we run:
#
#   * dependency names must EXIST in the target distro's repositories. They
#     differ per distro (libgtk-3-0 vs gtk3), and Ubuntu 24.04's 64-bit time_t
#     transition renamed several, which is why the deb declares alternatives
#     (`libgtk-3-0 | libgtk-3-0t64`). Ubuntu 24.04 is used deliberately here:
#     it is the release that proves the alternative resolves.
#   * the .desktop entry electron-builder generates, including the
#     StartupWMClass that must equal Electron's app_id for a desktop
#     environment to associate the running window with the launcher.
#   * the maintainer scripts, which place the /usr/bin launcher and remove it.
#   * the beacon distribution stamp, which must name THIS format. One backend
#     tree is packaged three times, so a single stamp would label one artifact
#     as another -- the exact mislabel scripts/stamp-distribution.sh exists to
#     prevent, and it is only observable from inside an installed package.
#
# A wrong dependency name produces a package that builds green and refuses to
# install, which no other gate in this repository would catch.
#
# Usage: smoke-linux-packages.sh <dist-dir>
set -euo pipefail

DIST_DIR="${1:?usage: smoke-linux-packages.sh <dir containing the built .deb and .rpm>}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to smoke-install the packages" >&2
  exit 1
fi

# Exactly one artifact per format. Zero means the build silently dropped it (a
# glob that no longer matches); more than one means an ambiguous input, and
# picking arbitrarily would test bytes nobody ships.
resolve_one() {
  local ext="$1" found
  mapfile -t found < <(find "$DIST_DIR" -maxdepth 1 -type f -name "*.${ext}")
  if [ "${#found[@]}" -ne 1 ]; then
    echo "ERROR: expected exactly one .${ext} in ${DIST_DIR}, found ${#found[@]}" >&2
    printf '  %s\n' "${found[@]}" >&2
    exit 1
  fi
  printf '%s' "$(basename "${found[0]}")"
}

DEB_NAME="$(resolve_one deb)"
RPM_NAME="$(resolve_one rpm)"
ABS_DIST="$(cd "$DIST_DIR" && pwd)"

# Shared assertions, run inside the container after the package manager has
# installed the package. The per-format beacon stamp is asserted by each caller,
# because the expected value differs per format.
common_asserts() {
  cat <<'ASSERTS'
    # The launcher symlink a maintainer script installs.
    test -x /usr/bin/kirocrew-desktop
    entry=/usr/share/applications/kirocrew-desktop.desktop
    test -f "$entry"
    # Window association: this key must equal Electron's app_id, which
    # electron-builder derives from package.json desktopName. A mismatch leaves
    # the window unassociated with the launcher icon -- silently, at runtime.
    grep -q "^StartupWMClass=kirocrew-desktop$" "$entry"
    grep -q "^Exec=/opt/KiroCrew/kirocrew-desktop" "$entry"
    # The bundled CLI the fixed install path makes durable. This is what makes
    # `kirocrew service install` reachable, which an AppImage cannot offer.
    test -x /opt/KiroCrew/resources/backend-dist/kirocrew-backend/bin/kirocrew
ASSERTS
}

echo "▶ Installing ${DEB_NAME} on Ubuntu 24.04 (the t64 release)…"
docker run --rm -v "${ABS_DIST}:/dist:ro" -w /dist ubuntu:24.04 bash -euxc "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends './${DEB_NAME}'
$(common_asserts)
  grep -q 'DISTRIBUTION = \"deb\"' \
    /opt/KiroCrew/resources/backend-dist/kirocrew-backend/lib/python3.12/site-packages/kiro_crew/_build_info.py
  # \`dpkg -r\` for the same reason as the rpm side: assert OUR package's removal
  # and its maintainer script, not the package manager's dependency bookkeeping.
  dpkg -r kirocrew
  test ! -e /usr/bin/kirocrew-desktop
"

echo "▶ Installing ${RPM_NAME} on Amazon Linux 2023…"
docker run --rm -v "${ABS_DIST}:/dist:ro" -w /dist amazonlinux:2023 bash -euxc "
  dnf install -y './${RPM_NAME}'
$(common_asserts)
  grep -q 'DISTRIBUTION = \"rpm\"' \
    /opt/KiroCrew/resources/backend-dist/kirocrew-backend/lib/python3.12/site-packages/kiro_crew/_build_info.py
  # \`rpm -e\` rather than \`dnf remove\`: dnf also plans the removal of
  # now-unneeded DEPENDENCIES, and in a bare container several of ours are
  # reverse-depended on by protected packages (systemd-udev), so it refuses the
  # whole transaction. What this step verifies is OUR package's own removal and
  # that its scriptlet takes the launcher symlink with it -- not dnf's dependency
  # bookkeeping, which is not ours to assert.
  rpm -e kirocrew
  test ! -e /usr/bin/kirocrew-desktop
"

echo "✅ Both Linux packages install, register, and uninstall cleanly."
