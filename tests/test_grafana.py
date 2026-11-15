"""The Grafana dashboard only uses series that tarvos-telemetry exports."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def exported() -> set[str]:
    names = set()
    for go in (ROOT / "telemetry/internal").rglob("*.go"):
        text = go.read_text()
        names |= {"tarvos" + m for m in re.findall(r'namespace\s*\+\s*"(_[a-z0-9_]+)"', text)}
        names |= {"tarvos_" + m for m in re.findall(r'fam\("([a-z0-9_]+)"', text)}
    return names


def test_dashboard_series_exist():
    names = exported()
    assert "tarvos_container_cpu_seconds_total" in names and "tarvos_bgp_neighbor_up" in names
    dashboard = json.loads((ROOT / "docs/grafana/tarvos.json").read_text())
    used = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            used |= set(re.findall(r"tarvos_[a-z0-9_]+", target["expr"]))
    assert used, "no queries found"
    histogram = {n + s for n in names for s in ("_bucket", "_sum", "_count")}
    assert used - names - histogram == set()
    assert any(v["name"] == "node" for v in dashboard["templating"]["list"])


def test_readme_documents_every_series():
    readme = (ROOT / "README.md").read_text()
    missing = []
    for name in sorted(exported()):
        if f"`{name}`" not in readme:
            missing.append(name)
    assert missing == []
