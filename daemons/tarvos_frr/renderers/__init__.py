"""Renderers: YANG data -> FRR configuration owned by Tarvos.

Order matters for frr-reload only when adding new blocks: policy objects first, then
BFD, then the protocols that reference them.
"""

from __future__ import annotations

from tarvos_frr.renderers.base import ConfigError, Renderer
from tarvos_frr.renderers.bfd import BfdRenderer
from tarvos_frr.renderers.bgp import BgpRenderer
from tarvos_frr.renderers.isis import IsisRenderer
from tarvos_frr.renderers.ospf import Ospf6Renderer, OspfRenderer
from tarvos_frr.renderers.policy import FilterRenderer, RouteMapRenderer
from tarvos_frr.renderers.raw import RAW_DAEMONS, RawRenderer

__all__ = ["ConfigError", "Renderer", "all_renderers"]


def all_renderers() -> list[Renderer]:
    return [
        FilterRenderer(),
        RouteMapRenderer(),
        BfdRenderer(),
        BgpRenderer(),
        OspfRenderer(),
        Ospf6Renderer(),
        IsisRenderer(),
        *(RawRenderer(daemon) for daemon in RAW_DAEMONS),
    ]
