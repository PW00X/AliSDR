"""Interactive launcher for the portable Windows worker."""
import getpass
import json
import os
import sys
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from alisdr.config import CONFIG_DIR, atomic_write_text
from alisdr.worker import main as run_worker


def receiver_url(value: str) -> str:
    value = value.strip()
    explicit_scheme = "://" in value
    parsed = urlsplit(value if explicit_scheme else f"http://{value}")
    port = parsed.port
    if (not parsed.hostname or parsed.scheme not in ("http", "https")
            or parsed.username is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() or c == "\\" for c in parsed.netloc)):
        raise ValueError("Enter an AliSDR address, e.g. http://192.168.1.10:2424")
    address = parsed.netloc if explicit_scheme or port is not None else f"{parsed.netloc}:2424"
    return urlunsplit((parsed.scheme, address, "", "", ""))


def session(url: str, password: str) -> dict:
    headers = {"Authorization": f"Bearer {password}"} if password else {}
    with urlopen(Request(f"{url}/api/session", headers=headers), timeout=5) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("mode") != "server" or "authenticated" not in result:
        raise ValueError("This address did not respond as an AliSDR server.")
    return result


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: Start AliSDR Worker.cmd [receiver address]", file=sys.stderr)
        return 2
    address_file = CONFIG_DIR / "windows-worker-url.txt"
    try:
        saved = address_file.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        saved = ""
    try:
        print("AliSDR transcription worker for Windows")
        hint = f" [{saved}]" if saved else " (e.g. 192.168.1.10)"
        address = sys.argv[1] if len(sys.argv) == 2 else input(f"Receiver address{hint}: ").strip() or saved
        url = receiver_url(address)
        print(f"Checking {url}...", flush=True)
        password = os.environ.get("ALISDR_ACCESS_TOKEN", "")
        if not session(url, password)["authenticated"]:
            password = getpass.getpass("Receiver password: ")
            if not session(url, password)["authenticated"]:
                raise ValueError("The receiver did not accept that password.")
        try:
            atomic_write_text(address_file, url + "\n")
        except OSError as exc:
            print(f"Could not remember the receiver address: {exc}", file=sys.stderr)
        os.environ["ALISDR_ACCESS_TOKEN"] = password
        print("Keep this window open. Manage this computer under Add-ons > Transcription & computers.", flush=True)
        sys.argv = [sys.argv[0], url]
        return run_worker()
    except (OSError, URLError, ValueError) as exc:
        print(f"Could not start the worker: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
