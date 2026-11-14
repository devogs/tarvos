#!/usr/bin/env python3
"""Generate docs/grafana/tarvos.json (the Tarvos Grafana dashboard).

Edit the panel list here and re-run: python3 docs/grafana/generate.py
Only series exported by tarvos-telemetry are used (checked by tests/test_grafana.py).
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
N = 'instance=~"$node"'
panels: list[dict] = []
state = {"y": 0, "x": 0, "id": 1}


def row(title: str) -> None:
    if state["x"]:
        state["y"] += 8
        state["x"] = 0
    panels.append({"type": "row", "title": title, "id": state["id"], "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": state["y"]}, "panels": []})
    state["id"] += 1
    state["y"] += 1


def panel(title: str, targets: list[tuple[str, str]], unit: str = "short", kind: str = "timeseries",
          width: int = 8, stack: bool = False, description: str = "") -> None:
    if state["x"] + width > 24:
        state["x"] = 0
        state["y"] += 8
    p = {
        "type": kind, "title": title, "id": state["id"], "datasource": DS,
        "description": description,
        "gridPos": {"h": 8, "w": width, "x": state["x"], "y": state["y"]},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
                    for i, (expr, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }
    if kind == "timeseries" and stack:
        p["fieldConfig"]["defaults"]["custom"] = {"stacking": {"mode": "normal"}, "fillOpacity": 30}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"}
    panels.append(p)
    state["id"] += 1
    state["x"] += width


row("Node resources (container cgroup, not the host)")
panel("CPU (cores)", [(f"rate(tarvos_container_cpu_seconds_total{{{N}}}[1m])", "{{instance}}")],
      unit="short", description="CPU used by each node's container.")
panel("Memory", [(f'tarvos_container_memory_bytes{{{N},type="usage"}}', "{{instance}}"),
                 (f"tarvos_container_memory_limit_bytes{{{N}}}", "{{instance}} limit")], unit="bytes")
panel("Memory by type", [(f'tarvos_container_memory_bytes{{{N},type!="usage"}}',
                          "{{instance}} {{type}}")], unit="bytes", stack=True)
panel("Daemon memory (top 10)", [(f"topk(10, tarvos_process_resident_memory_bytes{{{N}}})",
                                  "{{instance}} {{process}}")], unit="bytes")
panel("Daemon CPU (top 10)", [(f"topk(10, rate(tarvos_process_cpu_seconds_total{{{N}}}[1m]))",
                               "{{instance}} {{process}}")], unit="short")
panel("PIDs and uptime", [(f"tarvos_container_pids{{{N}}}", "{{instance}} pids"),
                          (f"tarvos_node_uptime_seconds{{{N}}} / 3600", "{{instance}} uptime (h)")])

row("Interfaces")
panel("Throughput rx (bit/s)", [(f'rate(tarvos_interface_bytes_total{{{N},direction="rx"}}[1m]) * 8',
                                 "{{instance}} {{interface}}")], unit="bps")
panel("Throughput tx (bit/s)", [(f'rate(tarvos_interface_bytes_total{{{N},direction="tx"}}[1m]) * 8',
                                 "{{instance}} {{interface}}")], unit="bps")
panel("Interfaces down", [(f'tarvos_interface_up{{{N},kind!="vrf"}} == 0', "{{instance}} {{interface}}")],
      kind="table", description="Interfaces that are not operationally up.")
panel("Drops by reason (kernel)", [(f"sum by (instance, reason) (rate(tarvos_interface_drops_total{{{N}}}[5m]))",
                                    "{{instance}} {{reason}}")], unit="pps")
panel("Netdev errors and drops", [
    (f"rate(tarvos_interface_netdev_errors_total{{{N}}}[5m])", "{{instance}} {{interface}} {{direction}} errors"),
    (f"rate(tarvos_interface_netdev_dropped_total{{{N}}}[5m])", "{{instance}} {{interface}} {{direction}} dropped"),
], unit="pps")
panel("VXLAN per VNI (bit/s)", [(f"rate(tarvos_vni_bytes_total{{{N}}}[1m]) * 8",
                                 "{{instance}} vni {{vni}} {{direction}}")], unit="bps")

row("BGP and EVPN")
panel("BGP sessions established / configured", [
    (f'sum by (instance) (tarvos_bgp_neighbors{{{N},state="established"}})', "{{instance}} established"),
    (f'sum by (instance) (tarvos_bgp_neighbors{{{N},state="configured"}})', "{{instance}} configured"),
])
panel("BGP neighbours down", [(f"tarvos_bgp_neighbor_up{{{N}}} == 0",
                               "{{instance}} {{vrf}} {{neighbor}} AS{{remote_as}}")], kind="table")
panel("Prefixes received", [(f"tarvos_bgp_prefixes_received{{{N}}}",
                             "{{instance}} {{neighbor}} {{afi_safi}}")])
panel("EVPN routes by type", [(f"tarvos_evpn_routes{{{N}}}", "{{instance}} type-{{route_type}}")],
      stack=True)
panel("VNIs: remote VTEPs and MACs", [
    (f"tarvos_evpn_vni_remote_vteps{{{N}}}", "{{instance}} vni {{vni}} VTEPs"),
    (f"tarvos_evpn_vni_macs{{{N}}}", "{{instance}} vni {{vni}} MACs"),
])
panel("BGP updates and session resets", [
    (f'rate(tarvos_bgp_messages_total{{{N},type="update"}}[5m])', "{{instance}} {{neighbor}} {{direction}}"),
    (f"increase(tarvos_bgp_session_resets_total{{{N}}}[1h])", "{{instance}} {{neighbor}} resets/h"),
])

row("Control plane, QoS, CoPP, AAA")
panel("Control-plane packets", [(f"sum by (instance, protocol) (rate(tarvos_control_plane_packets_total{{{N}}}[5m]))",
                                 "{{instance}} {{protocol}}")], unit="pps")
panel("QoS class throughput", [(f"rate(tarvos_qos_class_bytes_total{{{N}}}[1m]) * 8",
                                "{{instance}} {{interface}} class {{class}}")], unit="bps")
panel("QoS drops and overlimits", [
    (f"rate(tarvos_qos_class_drops_total{{{N}}}[5m])", "{{instance}} {{interface}} {{class}} drops"),
    (f"rate(tarvos_qos_class_overlimits_total{{{N}}}[5m])", "{{instance}} {{interface}} {{class}} overlimits"),
], unit="pps")
panel("CoPP matched and dropped", [
    (f"rate(tarvos_copp_matched_packets_total{{{N}}}[1m])", "{{instance}} {{rule}} matched"),
    (f"rate(tarvos_copp_dropped_packets_total{{{N}}}[1m])", "{{instance}} {{rule}} dropped"),
], unit="pps")
panel("TACACS+ servers reachable", [(f"tarvos_aaa_server_reachable{{{N}}}", "{{instance}} {{server}}")])
panel("Authentications", [(f"sum by (instance, method, result) (increase(tarvos_aaa_authentications_total{{{N}}}[1h]))",
                           "{{instance}} {{method}} {{result}}")])

row("Telemetry health")
panel("Telemetry and state sources up", [
    (f"tarvos_telemetry_up{{{N}}}", "{{instance}} agent"),
    (f"tarvos_telemetry_state_up{{{N}}}", "{{instance}} {{source}}"),
])
panel("Scrape duration", [(f"tarvos_telemetry_scrape_duration_seconds{{{N}}}", "{{instance}}")], unit="s")
panel("eBPF map memory", [(f"sum by (instance) (tarvos_ebpf_map_memory_bytes{{{N}}})", "{{instance}}")],
      unit="bytes")
panel("Build", [(f"tarvos_build_info{{{N}}}", "{{instance}} {{version}} FRR {{frr_version}}")], kind="table",
      width=24)

dashboard = {
    "title": "Tarvos",
    "uid": "tarvos-nodes",
    "description": "Tarvos nodes: container resources, interfaces, BGP/EVPN, control plane, QoS/CoPP/AAA.",
    "tags": ["tarvos", "network"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Prometheus",
         "current": {}, "hide": 0},
        {"name": "node", "type": "query", "label": "Node", "datasource": DS,
         "query": {"query": "label_values(tarvos_build_info, instance)", "refId": "node"},
         "definition": "label_values(tarvos_build_info, instance)", "refresh": 2,
         "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"}, "hide": 0},
    ]},
    "panels": panels,
}
out = Path(__file__).with_name("tarvos.json")
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(panels)} panels)")
