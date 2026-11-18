#!/bin/sh
# Installs or updates the AliSDR receiver from this checkout (run as root
# on the receiver):  sudo packaging/remote-server/install.sh
# Safe to re-run: code is synced to /opt/alisdr, settings and recordings
# are kept, and a running service is restarted.
set -eu
SRC=$(cd "$(dirname "$0")/../.." && pwd)
DEST=/opt/alisdr
HOME_DIR=/var/lib/alisdr
UNIT=alisdr-server.service
LEGACY_HOME=/var/lib/falconsdr
UNIT_DIR=/etc/systemd/system

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

# A fresh Debian/Raspberry Pi OS Lite lacks some of these.
if command -v apt-get >/dev/null && ! { command -v rsync && python3 -c "import ensurepip"; } >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync python3-venv >/dev/null
fi

# HackRF/Airspy without SoapySDR (experimental) use the system libraries - small, and harmless without the hardware.
if command -v apt-get >/dev/null && ! dpkg -s libhackrf0 libairspy0 >/dev/null 2>&1; then
    { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libhackrf0 libairspy0 >/dev/null; } ||
        echo "note: could not install libhackrf0 / libairspy0 (only needed for a HackRF or Airspy without SoapySDR)"
fi

getent group plugdev >/dev/null || groupadd --system plugdev
if ! id alisdr >/dev/null 2>&1; then
    useradd --system --home-dir "$HOME_DIR" --no-create-home --shell /usr/sbin/nologin --groups plugdev alisdr
fi
mkdir -p "$DEST"
rsync -a --delete --exclude .venv --exclude .git --exclude '__pycache__' \
    --exclude 'packaging/linux-deb/build*' --exclude 'packaging/linux-deb/out' "$SRC/" "$DEST/"
[ -x "$DEST/.venv/bin/python" ] || python3 -m venv --system-site-packages "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --upgrade -r "$DEST/requirements.txt"
sh "$SRC/packaging/dab/install.sh" "$DEST/.venv/lib"

install -D -m 644 "$SRC/packaging/linux-deb/60-rtl-sdr.rules" /etc/udev/rules.d/60-rtl-sdr.rules
install -D -m 644 "$SRC/packaging/linux-deb/blacklist-rtl-dvb.conf" /etc/modprobe.d/blacklist-rtl-dvb.conf
command -v udevadm >/dev/null && timeout 5 udevadm control --reload-rules >/dev/null 2>&1 || true

install -m 644 "$SRC/packaging/remote-server/$UNIT" "$UNIT_DIR/$UNIT"
# Record the deployed source revision before the new process imports the web server.
revision=$(git -C "$SRC" rev-parse --verify HEAD 2>/dev/null || true)
if [ -n "$revision" ]; then
    printf '%s\n' "$revision" > "$DEST/.alisdr-revision"
    chmod 644 "$DEST/.alisdr-revision"
fi
# Retire the standalone legacy service before accepting its name as an alias.
# Leave legacy drop-ins in place: systemd also reads drop-ins for unit aliases.
for legacy_unit in alisdr-receiver.service falconsdr-receiver.service; do
    legacy_path="$UNIT_DIR/$legacy_unit"
    if { [ -e "$legacy_path" ] || [ -L "$legacy_path" ]; } && ! [ "$legacy_path" -ef "$UNIT_DIR/$UNIT" ]; then
        if [ -f "$legacy_path" ]; then
            systemctl stop "$legacy_unit"
            systemctl disable --quiet "$legacy_unit"
        fi
        # Preserve a locally edited unit for reference after the migration.
        if [ -e "$legacy_path" ] || [ -L "$legacy_path" ]; then
            mv -f "$legacy_path" "$legacy_path.pre-alisdr"
        fi
    fi
done
# Move the service home only after the old receiver has closed any recordings.
if [ -d "$LEGACY_HOME" ] && [ ! -e "$HOME_DIR" ]; then
    mv "$LEGACY_HOME" "$HOME_DIR"
fi
mkdir -p "$HOME_DIR"

# Releases before the service had its own account stored data under root.
for sub in .config/FalconSDR .local/share/FalconSDR; do
    if [ -d "/root/$sub" ] && [ ! -e "$HOME_DIR/$sub" ] && [ ! -e "$HOME_DIR/${sub%/*}/AliSDR" ]; then
        mkdir -p "$HOME_DIR/$(dirname "$sub")"
        cp -a "/root/$sub" "$HOME_DIR/$sub"
    fi
done

# Map legacy paths before restoring ownership or creating a recording folder.
REC_DIR=$(cd "$DEST" && HOME="$HOME_DIR" .venv/bin/python -c 'from alisdr.config import AppConfig; print(AppConfig.load().recording.output_dir)')
mkdir -p "$REC_DIR"
chown -R alisdr:alisdr "$HOME_DIR" "$DEST/.venv" "$REC_DIR"
systemctl daemon-reload
systemctl enable --quiet "$UNIT"
systemctl restart "$UNIT"  # starts it, or restarts it onto the new code
echo "Web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8765/"
