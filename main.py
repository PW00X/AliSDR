"""AliSDR headless receiver. Open the server address in a remote browser."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Transcription may live in this repo's own .venv (PEP 668 fallback - see
# transcription_install.py): re-exec into it before importing anything heavy.
from alisdr.venv_paths import VENV_DIR, VENV_PYTHON
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV_DIR.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import argparse  # noqa: E402
import faulthandler  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402

from alisdr import __version__  # noqa: E402
from alisdr.config import CONFIG_DIR, AppConfig, migrate_legacy_home  # noqa: E402

DEFAULT_PORT = 2424


def main() -> int:
    parser = argparse.ArgumentParser(description="AliSDR")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)  # older service files
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--start", action="store_true", help="start scanning immediately")
    parser.add_argument("--version", action="version", version=f"AliSDR {__version__}")
    parser.add_argument("--self-test", action="store_true", help="check this install has everything it needs, then exit")
    parser.add_argument("--list-native-sdr", action="store_true", help=argparse.SUPPRESS)  # asked by the server itself
    parser.add_argument("--probe-sdr", nargs="?", const="", default=None, metavar="DRIVER",
                        help="measure the receiver with the scanner stopped, print a report, then exit")
    args = parser.parse_args()

    if args.list_native_sdr:
        import json
        from alisdr import sdr_native
        print(json.dumps(sdr_native.enumerate_here()), flush=True)
        return 0
    migrate_legacy_home()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        from alisdr import selftest
        return selftest.run()
    if args.probe_sdr is not None:
        from alisdr import sdr_probe
        return sdr_probe.run(args.probe_sdr)
    faulthandler.enable(file=(CONFIG_DIR / "crash.log").open("a", encoding="utf-8", buffering=1), all_threads=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from alisdr import i18n
    from alisdr.web.server import App, serve
    from alisdr.worker import Worker

    from alisdr.controller import Controller

    config = AppConfig.load()
    i18n.set_language(config.language)
    controller = Controller(config)
    app = App(config, controller, config.access_token)
    try:
        httpd = serve(app, args.host, args.port)
    except OSError as exc:
        print(f"Can't listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        controller.shutdown()
        return 1

    stop = threading.Event()
    restart = threading.Event()

    def request_restart() -> None:
        restart.set()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    worker_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
    worker_host = f"[{worker_host}]" if ":" in worker_host else worker_host
    worker = Worker(f"http://{worker_host}:{httpd.server_port}", lambda: app.token,
                    name=os.environ.get("ALISDR_WORKER_NAME", ""), embedded=True, on_restart=request_restart,
                    decoders_paused=controller.transcription_paused)
    controller.before_dab = worker.pause_transcription
    threading.Thread(target=controller.start_background, daemon=True, name="library-start").start()
    try:
        worker.start()
        advertised = socket.gethostname() if args.host in ("0.0.0.0", "::") else worker_host
        print(f"AliSDR server: http://{advertised}:{httpd.server_port}/", flush=True)
        if args.start:
            controller.start()
        stop.wait()
    finally:
        worker.stop()
        if worker.ident is not None:
            worker.join(timeout=2)
        httpd.shutdown()
        httpd.server_close()
        controller.shutdown()
    if restart.is_set():
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
