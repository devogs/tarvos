#!/usr/bin/env python3
"""tarvos-kernel-sync: the kernel backends of tarvos-aaa (PAM/TACACS+), tarvos-qos (tc)
and tarvos-nftables (CoPP) in one process and one Sysrepo connection.

Each backend lives in its own module (aaa_sync, qos_sync, nft_sync, also runnable on
their own) and is skipped when its YANG module is not installed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from aaa_sync import AaaBackend  # noqa: E402
from nft_sync import NftBackend  # noqa: E402
from qos_sync import QosBackend  # noqa: E402
from tarvos_backend import serve  # noqa: E402


def main() -> int:
    return serve(AaaBackend(), QosBackend(), NftBackend())


if __name__ == "__main__":
    sys.exit(main())
