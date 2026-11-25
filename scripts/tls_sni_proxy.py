#!/usr/bin/env python3
"""stdin/stdout TCP shim: TLS connect to ip:port with SNI host. For SSH over TLS."""
from __future__ import annotations

import socket
import ssl
import sys
import threading


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: tls_sni_proxy.py IP SNI_HOST PORT", file=sys.stderr)
        return 2
    ip, host, port_s = sys.argv[1], sys.argv[2], sys.argv[3]
    port = int(port_s)
    raw = socket.create_connection((ip, port), timeout=20)
    ctx = ssl.create_default_context()
    ss = ctx.wrap_socket(raw, server_hostname=host)
    ss.settimeout(None)
    inf = sys.stdin.buffer
    out = sys.stdout.buffer

    def to_net() -> None:
        try:
            while True:
                data = inf.read(16384)
                if not data:
                    break
                ss.sendall(data)
        except Exception:
            pass
        try:
            ss.shutdown(socket.SHUT_WR)
        except Exception:
            pass

    def to_local() -> None:
        try:
            while True:
                data = ss.recv(16384)
                if not data:
                    break
                out.write(data)
                out.flush()
        except Exception:
            pass

    t1 = threading.Thread(target=to_net, daemon=True)
    t2 = threading.Thread(target=to_local, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    try:
        ss.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
