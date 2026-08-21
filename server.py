"""Kira entry point.

The implementation lives in the :mod:`kira` package; this shim keeps
``python server.py`` and ``from server import build_server`` working.
"""
from kira import APP_NAME, APP_VERSION  # noqa: F401
from kira.api import KiraHTTPServer, KiraRequestHandler, discover_local_ip  # noqa: F401
from kira.errors import KiraError  # noqa: F401
from kira.main import build_server, main  # noqa: F401
from kira.store import KiraStore  # noqa: F401

if __name__ == "__main__":
    main()
