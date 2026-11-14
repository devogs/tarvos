#!/usr/bin/env python3
"""Test peer for tools.sh: a DNS resolver (UDP 53) and an HTTP server (TCP 8080).

Answers A queries for the names given on the command line (name=ipv4 ...) and NXDOMAIN
otherwise; HTTP GET returns "tarvos-peer <label>". One peer sits in each VRF of the node
under test, so a successful lookup or fetch proves the request left through that VRF.

Usage: peer_server.py LABEL [name=ipv4 ...]
"""

import http.server
import ipaddress
import socket
import socketserver
import struct
import sys
import threading


def parse_qname(packet: bytes, offset: int) -> tuple[str, int]:
    labels = []
    while True:
        length = packet[offset]
        offset += 1
        if length == 0:
            return ".".join(labels).lower(), offset
        labels.append(packet[offset : offset + length].decode("ascii", "replace"))
        offset += length


def answer(packet: bytes, records: dict[str, str]) -> bytes:
    ident, _flags, qdcount = struct.unpack("!HHH", packet[:6])
    if qdcount != 1:
        return struct.pack("!HHHHHH", ident, 0x8184, 0, 0, 0, 0)  # NOTIMP
    name, end = parse_qname(packet, 12)
    qtype, _qclass = struct.unpack("!HH", packet[end : end + 4])
    question = packet[12 : end + 4]
    address = records.get(name.rstrip("."))
    if address is None:
        return struct.pack("!HHHHHH", ident, 0x8183, 1, 0, 0, 0) + question  # NXDOMAIN
    if qtype != 1:  # not A: empty NOERROR answer
        return struct.pack("!HHHHHH", ident, 0x8180, 1, 0, 0, 0) + question
    rr = struct.pack("!HHHIH", 0xC00C, 1, 1, 30, 4) + ipaddress.IPv4Address(address).packed
    return struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0) + question + rr


def serve_dns(records: dict[str, str]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    while True:
        packet, client = sock.recvfrom(512)
        try:
            sock.sendto(answer(packet, records), client)
        except (IndexError, struct.error, ValueError):
            continue


def main() -> int:
    label = sys.argv[1]
    records = dict(arg.split("=", 1) for arg in sys.argv[2:])
    threading.Thread(target=serve_dns, args=(records,), daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = f"tarvos-peer {label}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
