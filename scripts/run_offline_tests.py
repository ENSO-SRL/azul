"""Run payment regressions without network, real credentials or a live DB."""
import os
from pathlib import Path
import socket
import sys
import threading


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update({"AWS_EC2_METADATA_DISABLED": "true", "AZUL_ENV": "sandbox",
        "API_KEY": "offline-tests", "DATABASE_URL": "postgresql+asyncpg://offline@localhost/offline"})
    local = threading.local()
    original_pair, original_connect = socket.socketpair, socket.socket.connect

    def denied(*args, **kwargs):
        raise RuntimeError("Network disabled for payment regressions; mock the dependency")

    def socketpair(*args, **kwargs):
        local.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.socketpair = False

    def connect(sock, address):
        if getattr(local, "socketpair", False) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return denied()

    socket.socketpair, socket.socket.connect = socketpair, connect
    socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied
    import pytest
    # Both files call the real payment processor; they require a separate,
    # explicitly authorized sandbox certification run.
    return pytest.main(sys.argv[1:] or ["tests", "--ignore=tests/test_sandbox_integration.py",
                                      "--ignore=tests/test_gateway.py", "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
