#!/bin/sh
# AliSDR one-line installer:
#
#   curl -fsSL https://raw.githubusercontent.com/PW00X/AliSDR/master/packaging/install.sh | bash -s -- --server
#
# Installs the fork from source by default. With ALISDR_REPO set to a
# repository that publishes releases, picks the matching artifact:
#   - Debian/Ubuntu  -> official .deb from GitHub Releases
#   - Fedora/openSUSE -> official .rpm from GitHub Releases
#   - Arch/CachyOS   -> source package from packaging/aur (makepkg)
#   - anything else (glibc-based, x86_64/aarch64) -> the AppImage from
#     GitHub Releases (no root needed)
#   - `--from-source` -> pip install from a git checkout instead (SoapySDR
#     hardware: HackRF/LimeSDR/Airspy/... - not in the packaged builds;
#     also the fallback for unsupported CPU architectures, musl-based
#     distros such as Alpine (the AppImage needs glibc and won't run
#     there), and releases that don't publish the matching asset yet).
#     Run it again from the folder that holds the checkout to update. It
#     pulls the checkout, resets the venv's settings and reinstalls the
#     requirements; restart AliSDR afterwards. HackRF/Airspy without
#     SoapySDR (experimental) need the system libraries libhackrf/libairspy,
#     which pip cannot provide: add --with-sdr-libs to have them installed
#     (apt: libhackrf0 libairspy0, dnf: hackrf airspyone_host; uses sudo).
#   - `--server` -> headless receiver: a checkout in /opt/alisdr-src,
#     packaging/remote-server/install.sh, systemd service started (Debian,
#     Ubuntu, Raspberry Pi OS). Use ALISDR_REF=<branch|tag> to pick a
#     version; re-run to update.
# Debian-based source and server installs build the native DAB decoder automatically.
#
# After installing, unplug and replug the RTL-SDR dongle once (udev rule),
# then run `alisdr` - the UI opens at http://127.0.0.1:8765.
set -eu

FORK="PW00X/AliSDR"
LEGACY_FORK="PW00X/FalconSDR"
REPO="${ALISDR_REPO:-${FALCONSDR_REPO:-$FORK}}"
VERSION="${ALISDR_VERSION:-${FALCONSDR_VERSION:-latest}}"
REF="${ALISDR_REF:-${FALCONSDR_REF:-}}"
FROM_SOURCE=0
SERVER=0
WITH_SDR_LIBS=0
for a in "$@"; do
  [ "$a" = "--with-sdr-libs" ] && WITH_SDR_LIBS=1
  [ "$a" = "--from-source" ] && FROM_SOURCE=1
  [ "$a" = "--server" ] && SERVER=1
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' (install it first)"; }

from_source_hint() {
  cat >&2 <<EOF
No prebuilt package fits this machine - install from source instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/master/packaging/install.sh | bash -s -- --from-source
(That path also supports SoapySDR hardware: HackRF/LimeSDR/Airspy/...)
EOF
}

# Only x86_64 and aarch64 have prebuilt assets. Anything else (e.g. a
# 32-bit ARM Raspberry Pi OS) must go --from-source rather than
# downloading a wrong-architecture .deb/.rpm.
check_arch() {
  case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) say "CPU architecture '$(uname -m)' has no prebuilt package."; from_source_hint; exit 1;;
  esac
}

# The AppImage (like the .deb/.rpm bundles) is built against glibc and
# won't run on musl-based distros such as Alpine.
is_musl() {
  [ -f /etc/alpine-release ] && return 0
  ldd --version 2>&1 | grep -qi musl && return 0
  return 1
}

# Curl wrapper that turns a missing release asset (404: the release
# doesn't publish that file yet) into a pointer at --from-source
# instead of a bare "curl: (22) 404".
fetch_asset() {
  url="$1"; out="$2"
  if ! curl -fsSL -o "$out" "$url"; then
    cat >&2 <<EOF
error: download failed:
  $url
That release asset may not exist (yet) - not every release publishes
every artifact (.deb/.rpm/AppImage). Check what's actually attached at
https://github.com/$REPO/releases, or install from source instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/master/packaging/install.sh | bash -s -- --from-source
EOF
    exit 1
  fi
}

release_tag() {
  # Resolves "latest" to e.g. v0.9.0 via the GitHub API; a pinned
  # ALISDR_VERSION=vX.Y.Z skips the network call.
  if [ "$VERSION" != "latest" ]; then printf '%s' "$VERSION"; return; fi
  need curl
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
    grep -m1 '"tag_name"' | cut -d'"' -f4
}

have_apt() { command -v apt-get >/dev/null 2>&1; }
have_dnf() { command -v dnf >/dev/null 2>&1; }
have_zypper() { command -v zypper >/dev/null 2>&1; }
have_pacman() { command -v pacman >/dev/null 2>&1; }

check_origin() {
  checkout="$1"; shift
  origin="$("$@" git -C "$checkout" remote get-url origin 2>/dev/null || true)"
  case "$REPO:$origin" in
    "$REPO:https://github.com/$REPO"|"$REPO:https://github.com/$REPO.git"|"$REPO:git@github.com:$REPO.git") ;;
    "$FORK:https://github.com/$LEGACY_FORK"|"$FORK:https://github.com/$LEGACY_FORK.git"|"$FORK:git@github.com:$LEGACY_FORK.git")
      "$@" git -C "$checkout" remote set-url origin "https://github.com/$FORK.git"
      ;;
    *) die "expected $REPO at $checkout. Move the existing checkout aside before installing this fork." ;;
  esac
}

install_deb() {
  tag="$1"
  check_arch
  case "$(uname -m)" in
    x86_64) deb_arch=amd64;;
    aarch64) deb_arch=arm64;;
  esac
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  say "Downloading AliSDR $tag (.deb)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/alisdr_${tag#v}_${deb_arch}.deb" \
    "$tmp/alisdr.deb"
  say "Installing (sudo apt install)..."
  sudo apt-get update -qq
  sudo apt install -y "$tmp/alisdr.deb"
}

install_rpm() {
  tag="$1"; ver="${tag#v}"
  check_arch
  arch="$(uname -m)"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  say "Downloading AliSDR $tag (.rpm)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/alisdr-${ver}-1.${arch}.rpm" \
    "$tmp/alisdr.rpm"
  if have_dnf; then sudo dnf install -y "$tmp/alisdr.rpm"
  elif have_zypper; then sudo zypper --non-interactive install --allow-unsigned-rpm "$tmp/alisdr.rpm"
  else sudo rpm -i "$tmp/alisdr.rpm"; fi
}

install_arch() {
  say "Building AliSDR with makepkg..."
  need git; need makepkg; need sudo
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  git clone --quiet --depth 1 "https://github.com/$FORK.git" "$tmp/AliSDR"
  (cd "$tmp/AliSDR/packaging/aur" && makepkg -si --noconfirm)
}

install_appimage() {
  tag="$1"
  check_arch
  arch="$(uname -m)"
  dest="${ALISDR_APPIMAGE:-${FALCONSDR_APPIMAGE:-$HOME/Applications/AliSDR-${tag}-${arch}.AppImage}}"
  mkdir -p "$(dirname "$dest")"
  say "Downloading AliSDR $tag (AppImage)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/AliSDR-${tag#v}-${arch}.AppImage" \
    "$dest"
  chmod +x "$dest"
  say "AppImage saved to $dest"
  if [ "$(id -u)" = "0" ]; then :
  elif [ ! -f /etc/udev/rules.d/60-rtl-sdr.rules ] && [ ! -f /usr/lib/udev/rules.d/60-rtl-sdr.rules ]; then
    cat <<EOF

RTL-SDR USB access needs one manual step (AppImages install nothing):
  sudo curl -fsSL -o /etc/udev/rules.d/60-rtl-sdr.rules \\
    https://raw.githubusercontent.com/$REPO/master/packaging/linux-deb/60-rtl-sdr.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
then unplug and replug the dongle once.
EOF
  fi
  say "Run it with: $dest"
}

# The HackRF/Airspy libraries are system packages (a pip wheel cannot carry them); the packaged builds bundle them.
install_sdr_libs() {
  SUDO=""
  if [ "$(id -u)" != "0" ]; then need sudo; SUDO=sudo; fi
  say "Installing the HackRF and Airspy libraries (--with-sdr-libs)..."
  if have_apt; then
    { $SUDO apt-get update -qq && $SUDO apt-get install -y libhackrf0 libairspy0; } ||
      say "Could not install libhackrf0 / libairspy0 - install them by hand if you use a HackRF or Airspy."
  elif have_dnf; then
    $SUDO dnf install -y hackrf airspyone_host ||
      say "Could not install hackrf / airspyone_host - install them by hand if you use a HackRF or Airspy."
  else
    say "--with-sdr-libs: no apt or dnf here - install libhackrf and libairspy with your package manager."
  fi
}

install_from_source() {
  say "Installing ${REPO##*/} from source (SoapySDR-capable path)..."
  need python3; need git
  # Started from inside a checkout (the folder the program runs from): use it, never clone a copy into it.
  if [ -f main.py ] && { [ -d alisdr ] || [ -d falconsdr ]; } && [ -e .git ]; then :
  else
    checkout=${REPO##*/}
    if [ "$REPO" = "$FORK" ] && [ ! -d "$checkout" ] && [ -d FalconSDR/.git ]; then
      checkout=FalconSDR
    fi
    if [ ! -d "$checkout" ]; then git clone "https://github.com/$REPO.git" "$checkout"; fi
    cd "$checkout"
  fi
  # A checkout that already exists is updated, not just reinstalled: this is also the update command.
  if [ -e .git ]; then
    check_origin .
    if git pull --ff-only -q 2>/dev/null; then :
    else say "Could not update the checkout (local changes, or not on a branch) - keeping the code as it is."; fi
  fi
  pkg_dir=alisdr
  [ -f "$pkg_dir/__init__.py" ] || pkg_dir=falconsdr
  version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$pkg_dir/__init__.py")
  # --system-site-packages: SoapySDR's Python module comes from the distribution (python3-soapysdr) and a
  # plain venv cannot see it - HackRF & co were silently not found (issue #9).
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install -r requirements.txt
  if [ "$WITH_SDR_LIBS" = "1" ]; then install_sdr_libs; fi
  if have_apt && [ -f packaging/dab/install.sh ]; then sh packaging/dab/install.sh .venv/lib; fi
  cat <<EOF

${REPO##*/} $version is ready. Start it with:
  .venv/bin/python main.py
(Already running? Stop it first, then start it again to use this version. To update later,
run this same command again from the folder that contains the checkout.)
(RTL-SDR udev rule: packaging/linux-deb/60-rtl-sdr.rules - see the README.
SoapySDR hardware: sudo apt install python3-soapysdr soapysdr-module-<device>, then restart.
HackRF/Airspy also work without SoapySDR (experimental): add --with-sdr-libs to this command,
or install libhackrf0 / libairspy0 yourself.)
EOF
}

install_server() {
  have_apt || die "--server needs a Debian-based system (Debian, Ubuntu, Raspberry Pi OS)"
  SUDO=""
  if [ "$(id -u)" != "0" ]; then need sudo; SUDO=sudo; fi
  src=/opt/alisdr-src
  if ! command -v git >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git ca-certificates
  fi
  if [ "$REPO" = "$FORK" ] && [ -z "$REF" ] && [ "$VERSION" = "latest" ]; then
    ref=master
  else
    ref="${REF:-$(release_tag)}"
  fi
  [ -n "$ref" ] || die "couldn't find the latest release (GitHub API) - set ALISDR_REF=v1.1.1 or similar"
  say "Fetching ${REPO##*/} ($ref)..."
  if [ -d "$src/.git" ]; then
    previous_origin="$($SUDO git -C "$src" remote get-url origin)"
    case "$REPO:$previous_origin" in
      "$FORK:https://github.com/rafafunt/FalconSDR"|"$FORK:https://github.com/rafafunt/FalconSDR.git"|"$FORK:git@github.com:rafafunt/FalconSDR.git")
        say "Updating the existing upstream receiver from $FORK."
        ;;
      *) check_origin "$src" $SUDO ;;
    esac
    $SUDO git -C "$src" fetch -q --depth 1 "https://github.com/$REPO.git" "$ref"
    $SUDO git -C "$src" checkout -q --force FETCH_HEAD
  else
    $SUDO git -c advice.detachedHead=false clone -q --depth 1 --branch "$ref" "https://github.com/$REPO.git" "$src"
  fi
  revision="$($SUDO git -C "$src" rev-parse --verify HEAD)"
  app=alisdr
  if [ -f "$src/packaging/remote-server/alisdr-server.service" ]; then
    unit=alisdr-server
  else
    # Older pinned checkouts built DAB separately and used the receiver name.
    [ -f "$src/alisdr/__init__.py" ] || app=falconsdr
    unit="$app-receiver"
    if [ -f "$src/packaging/dab/install.sh" ]; then
      $SUDO sh "$src/packaging/dab/install.sh" "/opt/$app/.venv/lib"
    fi
  fi
  $SUDO sh "$src/packaging/remote-server/install.sh" >/dev/null
  $SUDO systemctl enable --quiet --now "$unit.service"
  version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$src/$app/__init__.py")
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [ "$REPO" = "$FORK" ] && [ "$ref" = "master" ]; then
    retries=10
    while :; do
      session="$(curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:8765/api/session 2>/dev/null || true)"
      if printf '%s\n' "$session" | grep -Eq "\"revision\"[[:space:]]*:[[:space:]]*\"$revision\"" &&
         printf '%s\n' "$session" | grep -Eq '"mode"[[:space:]]*:[[:space:]]*"server"'; then
        break
      fi
      retries=$((retries - 1))
      [ "$retries" -gt 0 ] ||
        die "Port 8765 is not serving headless revision $revision. Check for an older receiver process and run: journalctl -u $unit -n 50 --no-pager"
      sleep 2
    done
  else
    sleep 2
  fi
  systemctl is-active --quiet "$unit.service" ||
    die "${REPO##*/} $version is installed, but the service didn't start - see: journalctl -u $unit"
  say ""
  say "${REPO##*/} $version ($revision) is installed and running (service $unit, starts on boot)."
  say "Open it from any device on your network:  http://${ip:-<server-ip>}:8765/"
  say "To update later, run the same command again."
}

main() {
  if [ "$SERVER" = "1" ]; then install_server; return; fi
  if [ "$FROM_SOURCE" = "1" ] || { [ "$REPO" = "$FORK" ] && [ "$VERSION" = "latest" ]; }; then
    install_from_source
    return
  fi
  # No prebuilt bundle runs here: musl-based distros (Alpine, ...) can't
  # execute the glibc-linked AppImage, and there is no musl asset.
  if is_musl; then
    say "musl-based distribution detected - the AppImage needs glibc and won't run here."
    from_source_hint; exit 1
  fi
  check_arch
  if have_apt; then install_deb "$(release_tag)"
  elif have_dnf || have_zypper; then install_rpm "$(release_tag)"
  elif have_pacman; then install_arch
  else install_appimage "$(release_tag)"; fi
  cat <<EOF

Unplug and replug the RTL-SDR dongle once (USB permission rule), then run:
  alisdr
The UI opens at http://127.0.0.1:8765.
EOF
}

main
