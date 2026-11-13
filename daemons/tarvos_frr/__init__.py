"""Tarvos FRR integration: NETCONF/YANG (Sysrepo) to FRR 10.7.

Two paths, chosen per YANG module:

* ``mgmtd``: FRR's own YANG modules for the daemons that are mgmtd backends (zebra,
  staticd, ripd, ripngd). The Sysrepo data is pushed to mgmtd unchanged, as one
  candidate/commit transaction (``mgmt edit`` + ``mgmt commit apply``).
* ``render``: the other daemons. Their configuration (FRR YANG where FRR has a usable
  model: frr-filter, frr-route-map, frr-bfdd; Tarvos YANG otherwise) is rendered into
  FRR configuration text and applied with FRR's frr-reload, which only sends the
  differences.

Everything here is pure Python except :mod:`tarvos_frr.vtysh`, so it can be unit tested.
"""
