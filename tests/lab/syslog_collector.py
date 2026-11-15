#!/usr/bin/env python3
"""Minimal syslog collector for the lab: UDP and TCP (octet-counted or newline framed)
on port 514; every message is printed on stdout prefixed with the sender address.

Usage: syslog_collector.py [--port 514]
"""

import argparse
import socket
import socketserver
import sys
import threading


def emit(source: str, message: bytes) -> None:
    text = message.decode(errors="replace").strip()
    if text:
        print(f"{source} {text}", flush=True)


class TCPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        source = self.client_address[0]
        buffer = b""
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while buffer:
                head = buffer.split(b" ", 1)[0]
                if head.isdigit() and b" " in buffer:  # RFC 6587 octet counting
                    size = int(head)
                    start = len(head) + 1
                    if len(buffer) < start + size:
                        break
                    emit(source, buffer[start : start + size])
                    buffer = buffer[start + size :]
                elif b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    emit(source, line)
                else:
                    break


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=514)
    args = parser.parse_args()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", args.port))  # noqa: S104 - lab fixture

    def udp_loop() -> None:
        while True:
            data, (host, _port) = udp.recvfrom(65536)
            emit(host, data)

    threading.Thread(target=udp_loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", args.port), TCPHandler) as server:  # noqa: S104
        print(f"syslog collector on udp/tcp {args.port}", file=sys.stderr, flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
