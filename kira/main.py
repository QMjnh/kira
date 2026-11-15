"""Kira entry point: argument parsing and server startup."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from . import APP_VERSION
from .api import APP_ROOT, KiraHTTPServer, discover_local_ip
from .app import KiraApp
from .errors import KiraError
from .writer import WriterLease

STATIC_ROOT = Path(__file__).resolve().parent.parent / "web"


def build_server(host: str, port: int, data_dir: Path) -> KiraHTTPServer:
    lease = WriterLease(data_dir.resolve())
    try:
        server = KiraHTTPServer((host, port), KiraApp(data_dir, APP_ROOT), STATIC_ROOT)
        server.writer_lease = lease
        return server
    except BaseException:
        lease.close()
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kira photo library")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to listen on")
    parser.add_argument("--port", type=int, default=8787, help="TCP port")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "kira-data",
        help="Folder for Kira's catalog cache, thumbnails and settings (not the photos)",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the desktop dashboard")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        server = build_server(args.host, args.port, args.data_dir)
    except KiraError as exc:
        raise SystemExit(str(exc)) from exc
    actual_port = server.server_address[1]
    local_url = f"http://127.0.0.1:{actual_port}"
    ipad_url = f"http://{discover_local_ip()}:{actual_port}"
    print()
    print(f"Kira {APP_VERSION} is running")
    print(f"Desktop dashboard: {local_url}")
    print(f"iPad address:   {ipad_url}")
    print(f"Pairing code:   {server.app.pair_code}")
    print(f"Data folder:    {server.app.root}")
    print(f"Library:        {server.app.library.root or 'not chosen yet'}")
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
