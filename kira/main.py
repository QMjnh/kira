"""Kira entry point: argument parsing and server startup."""
from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from . import APP_NAME, APP_VERSION
from .api import KiraHTTPServer, discover_local_ip
from .store import KiraStore

STATIC_ROOT = Path(__file__).resolve().parent.parent / "web"


def build_server(host: str, port: int, data_dir: Path) -> KiraHTTPServer:
    return KiraHTTPServer((host, port), KiraStore(data_dir), STATIC_ROOT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kira local photo transfer MVP")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to listen on")
    parser.add_argument("--port", type=int, default=8787, help="TCP port")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "kira-data",
        help="Folder where Kira stores jobs and returned edits",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the Dell dashboard")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = build_server(args.host, args.port, args.data_dir)
    actual_port = server.server_address[1]
    local_url = f"http://127.0.0.1:{actual_port}"
    ipad_url = f"http://{discover_local_ip()}:{actual_port}"
    print()
    print(f"Kira {APP_VERSION} is running")
    print(f"Dell dashboard: {local_url}")
    print(f"iPad address:   {ipad_url}")
    print(f"Pairing code:   {server.store.pair_code}")
    print(f"Data folder:    {server.store.root}")
    print("Press Ctrl+C to stop Kira.")
    print()
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(local_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Kira...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
