# Headless receiver

The receiver runs AliSDR as a service and serves its web UI on port
2424 to every device on the network: open `http://<receiver-ip>:2424/` in any
browser (PC, phone). There is no password by default; set one in
Settings → Access if other people share your network.

## Install / update

On the receiver (Debian, Ubuntu, Raspberry Pi OS - 64-bit recommended),
one line installs or updates it and starts the service:

```sh
curl -fsSL https://raw.githubusercontent.com/PW00X/AliSDR/master/packaging/install.sh | bash
```

It installs this fork's `master` branch and keeps its checkout in
`/opt/alisdr-src`. Set `ALISDR_REF=<tag or branch>` before `bash` to
choose a different ref. The installer builds AliSDR's
in-process DAB/DAB+ decoder on the receiver. Or, from a checkout of
this repository:

```sh
sudo packaging/remote-server/install.sh
```

Either way the service is started (and on every boot) and the address to
open is printed at the end.

The script copies the app to `/opt/alisdr`, creates its own Python
venv there, adds the unprivileged `alisdr` user (settings in
`/var/lib/alisdr/.config/AliSDR`, recordings by default in
`/var/lib/alisdr/AliSDR/recordings`), installs the RTL-SDR udev
rule and DVB-T driver blacklist, and the systemd unit. Re-run it after
`git pull` to update; settings and recordings stay. An older install that ran
as root has its settings moved over automatically the first time.

**Listen** opens in **Tune** when idle and **Scan** when the scanner is
running. To scan, select **Scan** in the tuner and press **Start**.
Stopping `alisdr-server.service` closes open
recordings cleanly. On upgrade, the installer replaces the standalone
receiver units with aliases to `alisdr-server.service` so existing scripts and unit overrides continue to work. The upgrade
moves the existing service data to `/var/lib/alisdr`.

## Headless transcription worker

Install the Python dependencies from a checkout on the worker host, then
connect it to the receiver:

```sh
python3 -m alisdr.worker http://<receiver-ip>:2424
```

Use `--ask-password` if the receiver requires a password, or set
`ALISDR_ACCESS_TOKEN` in the worker's service environment for unattended use.
Manage transcription, copies and library imports in the receiver's web app
under **Add-ons → Transcription & computers**.

Enable **Transcribe only when decoders are idle** to pause all workers
while the receiver decodes, including while tuning or playing DAB/DAB+.
Interrupted jobs return to the queue. With the switch off, workers can
transcribe during reception. Update remote workers to apply these rules
to jobs already running.

## USB passthrough

The receiver must see the RTL-SDR dongle. Proxmox LXC (on the Proxmox host,
replace `CTID`):

```sh
pct set CTID -usb0 host=0bda:2838
pct restart CTID
```

LXD:

```sh
lxc config device add INSTANCE rtlsdr usb vendorid=0bda productid=2838
```

The service reconnects by itself once the device appears. Check with
`rtl_test -t` and `systemctl status alisdr-server`.
