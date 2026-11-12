#!/usr/bin/env python3
"""Minimal TACACS+ server (RFC 8907) used only as a test fixture.

Supports PAP and ASCII login authentication, authorization (always PASS_ADD for known
users) and accounting (always SUCCESS). Not for production use.

Usage: tacacs_server.py --key KEY [--port 49] --user NAME:PASSWORD [--user ...]
"""

import argparse
import hashlib
import socketserver
import struct
import sys

HEADER = struct.Struct("!BBBBII")  # version, type, seq_no, flags, session_id, length
AUTHEN, AUTHOR, ACCT = 1, 2, 3
STATUS_PASS, STATUS_FAIL, STATUS_GETPASS = 1, 2, 4
AUTHEN_TYPE_ASCII, AUTHEN_TYPE_PAP = 1, 2


def crypt(key: bytes, session_id: int, version: int, seq_no: int, body: bytes) -> bytes:
    """(De)obfuscate a packet body with the MD5 pseudo-pad of RFC 8907 section 4.5."""
    prefix = struct.pack("!I", session_id) + key + bytes([version, seq_no])
    pad = b""
    previous = b""
    while len(pad) < len(body):
        previous = hashlib.md5(prefix + previous).digest()  # noqa: S324 - protocol mandated
        pad += previous
    return bytes(b ^ p for b, p in zip(body, pad, strict=False))


def split(data: bytes, *lengths: int) -> list[bytes]:
    parts, offset = [], 0
    for length in lengths:
        parts.append(data[offset : offset + length])
        offset += length
    return parts


def authen_reply(status: int, message: bytes = b"") -> bytes:
    return struct.pack("!BBHH", status, 0, len(message), 0) + message


class Handler(socketserver.BaseRequestHandler):
    users: dict[str, str]
    key: bytes

    def recv_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def send(self, version: int, kind: int, seq: int, session_id: int, body: bytes) -> None:
        encrypted = crypt(self.key, session_id, version, seq, body)
        self.request.sendall(HEADER.pack(version, kind, seq, 0, session_id, len(encrypted)))
        self.request.sendall(encrypted)

    def handle(self) -> None:
        pending_user = None
        try:
            while True:
                version, kind, seq, flags, session_id, length = HEADER.unpack(
                    self.recv_exact(HEADER.size)
                )
                raw = self.recv_exact(length)
                body = raw if flags & 0x01 else crypt(self.key, session_id, version, seq, raw)
                reply_seq = seq + 1
                if kind == AUTHEN:
                    pending_user, reply = self.authen(body, seq, pending_user)
                    self.send(version, kind, reply_seq, session_id, reply)
                elif kind == AUTHOR:
                    self.send(
                        version, kind, reply_seq, session_id, struct.pack("!BBHH", 1, 0, 0, 0)
                    )
                elif kind == ACCT:
                    self.send(version, kind, reply_seq, session_id, struct.pack("!HHB", 0, 0, 1))
        except (ConnectionError, struct.error):
            return

    def check(self, user: str, password: str) -> bool:
        return self.users.get(user) == password and bool(password)

    def authen(self, body: bytes, seq: int, pending_user: str | None) -> tuple[str | None, bytes]:
        if seq == 1:  # START
            _action, _priv, authen_type, _service, ulen, plen, rlen, dlen = body[:8]
            user, _port, _rem, data = split(body[8:], ulen, plen, rlen, dlen)
            name = user.decode(errors="replace")
            if authen_type == AUTHEN_TYPE_PAP:
                ok = self.check(name, data.decode(errors="replace"))
                return None, authen_reply(STATUS_PASS if ok else STATUS_FAIL)
            if authen_type == AUTHEN_TYPE_ASCII:
                return name, authen_reply(STATUS_GETPASS, b"Password: ")
            return None, authen_reply(STATUS_FAIL)
        # CONTINUE (ASCII): user_msg carries the password
        user_msg_len, data_len, _flags = struct.unpack("!HHB", body[:5])
        password = body[5 : 5 + user_msg_len].decode(errors="replace")
        ok = pending_user is not None and self.check(pending_user, password)
        return None, authen_reply(STATUS_PASS if ok else STATUS_FAIL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=49)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - test fixture
    parser.add_argument("--user", action="append", default=[], help="NAME:PASSWORD")
    args = parser.parse_args()
    Handler.key = args.key.encode()
    Handler.users = dict(u.split(":", 1) for u in args.user)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((args.host, args.port), Handler) as server:
        print(f"tacacs test server on {args.host}:{args.port}", file=sys.stderr, flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
