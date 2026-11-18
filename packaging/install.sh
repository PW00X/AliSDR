#!/bin/sh
# Install or update the AliSDR server from source on Debian, Ubuntu, or Raspberry Pi OS:
# curl -fsSL https://raw.githubusercontent.com/PW00X/AliSDR/master/packaging/install.sh | bash
set -eu

FORK="PW00X/AliSDR"
LEGACY_FORK="PW00X/FalconSDR"
REPO="${ALISDR_REPO:-${FALCONSDR_REPO:-$FORK}}"
VERSION="${ALISDR_VERSION:-${FALCONSDR_VERSION:-latest}}"
REF="${ALISDR_REF:-${FALCONSDR_REF:-}}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' (install it first)"; }

for arg in "$@"; do
  case "$arg" in
    --server|--from-source|--with-dab|--with-sdr-libs) ;; # Existing install commands
    *) die "unknown option: $arg" ;;
  esac
done

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

install_server() {
  need apt-get; need systemctl; need curl
  SUDO=""
  if [ "$(id -u)" != "0" ]; then need sudo; SUDO=sudo; fi
  src=/opt/alisdr-src
  if ! command -v git >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git ca-certificates
  fi
  ref="${REF:-master}"
  if [ -z "$REF" ] && [ "$VERSION" != "latest" ]; then ref="$VERSION"; fi
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
  $SUDO sh "$src/packaging/remote-server/install.sh"
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

install_server
