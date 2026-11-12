import socket
import struct
import threading
from socketserver import ThreadingTCPServer

import pytest
from tacacs_server import AUTHEN, HEADER, Handler, crypt

KEY = b"testkey"


@pytest.fixture
def server():
    Handler.key = KEY
    Handler.users = {"alice": "alicepw"}
    srv = ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def pap_login(port: int, user: str, password: str) -> int:
    u, d = user.encode(), password.encode()
    body = struct.pack("!BBBBBBBB", 1, 1, 2, 1, len(u), 0, 0, len(d)) + u + d
    session, version = 0x1234, 0xC1
    with socket.create_connection(("127.0.0.1", port)) as sock:
        enc = crypt(KEY, session, version, 1, body)
        sock.sendall(HEADER.pack(version, AUTHEN, 1, 0, session, len(enc)) + enc)
        _v, _t, seq, _f, sid, length = HEADER.unpack(sock.recv(HEADER.size))
        reply = crypt(KEY, sid, version, seq, sock.recv(length))
    return reply[0]


def test_pap_pass(server):
    assert pap_login(server, "alice", "alicepw") == 1


@pytest.mark.parametrize(("user", "pw"), [("alice", "bad"), ("mallory", "alicepw"), ("alice", "")])
def test_pap_fail(server, user, pw):
    assert pap_login(server, user, pw) == 2
