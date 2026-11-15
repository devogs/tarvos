#!/usr/bin/env python3
"""Tarvos day-2 campaign: change, break, restart, upgrade and audit a running fabric.

Runs against the lab of tests/lab/lab.sh (configured: lab.sh configure). Every case
records checks and measurements; the report (Markdown and JSON) lists them all.

Usage: day2.py [--short] [--only CASE[,CASE...]] [--report DIR] [--list]
  --short   the CI subset (a few minutes); without it the full campaign (~30 min)
Exit status: 1 when a check failed (KNOWN checks, documented limitations, do not fail).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from labkit import CASES, Lab, Recorder, Result


def report(results: list[Result], directory: Path, short: bool) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "day2.json").write_text(json.dumps([asdict(r) | {"status": r.status} for r in results], indent=2))
    lines = [
        f"# Tarvos day-2 campaign ({'short' if short else 'full'}, {time.strftime('%Y-%m-%d %H:%M')})",
        "",
        "| Area | Case | Result | Time | Measurements |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        measures = "; ".join(f"{k}: {v}" for k, v in r.measures.items())
        lines.append(f"| {r.area} | {r.case} | {r.status} | {r.seconds:.0f} s | {measures} |")
    lines += ["", "## Checks", ""]
    for r in results:
        lines.append(f"### {r.case} ({r.status})")
        if r.error:
            lines.append(f"- ERROR: `{r.error}`")
        for c in r.checks:
            mark = "ok" if c.ok else ("KNOWN" if c.known else "FAIL")
            extra = f" ({c.known})" if c.known else (f": {c.detail}" if c.detail and not c.ok else "")
            lines.append(f"- [{mark}] {c.name}{extra}")
        lines.append("")
    (directory / "day2.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    import cases_access  # noqa: F401 - registers cases
    import cases_fabric  # noqa: F401
    import cases_lifecycle  # noqa: F401

    parser = argparse.ArgumentParser()
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--report", default=str(Path(__file__).resolve().parent / "report"))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for area, name, short, _ in CASES:
            print(f"{area:12} {name:32} {'short' if short else ''}")
        return 0
    only = {n for n in args.only.split(",") if n}
    lab = Lab()
    results = []
    for area, name, short, function in CASES:
        if only and name not in only:
            continue
        if not only and args.short and not short:
            continue
        print(f"== {area}: {name}", flush=True)
        result = Result(area, name)
        start = time.monotonic()
        try:
            function(lab, Recorder(result))
        except Exception as exc:  # noqa: BLE001 - a case failing must not stop the campaign
            result.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        result.seconds = time.monotonic() - start
        print(f"   {result.status} ({result.seconds:.0f} s)", flush=True)
        results.append(result)
    report(results, Path(args.report), args.short)
    failed = [r.case for r in results if r.status == "FAIL"]
    print(f"\n{len(results)} cases, {len(failed)} failed{': ' + ', '.join(failed) if failed else ''}")
    print(f"report: {args.report}/day2.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
