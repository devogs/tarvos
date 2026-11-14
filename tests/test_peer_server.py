import struct

from peer_server import answer


def _query(name: str, qtype: int = 1) -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", qtype, 1)


def test_known_name_gets_an_a_record():
    reply = answer(_query("red.tarvos.test"), {"red.tarvos.test": "10.9.8.7"})
    ident, flags, _qd, an = struct.unpack("!HHHH", reply[:8])
    assert (ident, flags & 0xF, an) == (0x1234, 0, 1)
    assert reply[-4:] == bytes([10, 9, 8, 7])


def test_unknown_name_is_nxdomain():
    reply = answer(_query("nope.test"), {"red.tarvos.test": "10.9.8.7"})
    assert struct.unpack("!H", reply[2:4])[0] & 0xF == 3
