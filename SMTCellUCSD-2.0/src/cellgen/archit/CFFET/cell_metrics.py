"""Extract PPA-style metrics from CFFET .res + .log files.

Usage:
    python -m src.cellgen.archit.CFFET.cell_metrics \\
        output/PROBE3_CFFET_2F_3T_4530OF0/SH/result/INV_X1.res \\
        [--log output/.../logs/INV_X1.log]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field


LAYER_NAMES = {
    0: "BM1", 1: "BM0", 2: "BBOTPC", 3: "BTOPPC",
    4: "FBOTPC", 5: "FTOPPC", 6: "M0", 7: "M1", 8: "M2",
}


@dataclass
class CellMetrics:
    cell: str
    objective: float = 0.0
    cpp_cost: int = 0
    col: int = 0
    track: int = 3
    cpp_nm: float = 45.0
    m0p_nm: float = 24.0
    width_nm: float = 0.0
    height_nm: float = 0.0
    area_nm2: float = 0.0
    num_trans: int = 0
    tiers_used: set = field(default_factory=set)
    route_segs: int = 0
    via_hops: int = 0
    wire_nm: float = 0.0
    m0_wire_nm: float = 0.0
    bm0_wire_nm: float = 0.0
    obj_cpp: int | None = None
    obj_route: int | None = None
    solve_s: float | None = None

    def to_row(self) -> dict:
        return {
            "cell": self.cell,
            "obj": self.objective,
            "cpp": self.cpp_cost,
            "COL": self.col,
            "W(nm)": self.width_nm,
            "H(nm)": self.height_nm,
            "area(nm²)": self.area_nm2,
            "#T": self.num_trans,
            "tiers": ",".join(sorted(self.tiers_used)) or "-",
            "routes": self.route_segs,
            "vias": self.via_hops,
            "wire(nm)": round(self.wire_nm, 1),
            "M0(nm)": round(self.m0_wire_nm, 1),
            "BM0(nm)": round(self.bm0_wire_nm, 1),
            "obj_route": self.obj_route,
            "solve(s)": self.solve_s,
        }


def parse_res(path: str) -> CellMetrics:
    m = CellMetrics(cell=path.split("/")[-1].replace(".res", ""))
    mode = None
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("** Objective value:"):
                m.objective = float(line.split(":")[1])
            elif line.startswith("** CPP cost"):
                m.cpp_cost = int(line.split(":")[1])
            elif line.startswith("** Placement Result"):
                mode = "place"
                next(f, None)
                next(f, None)
                continue
            elif line.startswith("** Routing Result"):
                mode = "route"
                next(f, None)
                next(f, None)
                continue
            elif line.startswith("** Technology Parameters"):
                mode = "tech"
                continue
            elif line.startswith("**"):
                mode = None
                continue

            if mode == "place" and len(line.split()) >= 17:
                m.num_trans += 1
                m.tiers_used.add(line.split()[3])
            elif mode == "route" and "=>" in raw:
                parts = raw.split()
                try:
                    lu, ru, cu = int(parts[0]), float(parts[1]), float(parts[2])
                    lv, rv, cv = int(parts[5]), float(parts[6]), float(parts[7])
                except (ValueError, IndexError):
                    continue
                m.route_segs += 1
                if lu == lv:
                    length = abs(cv - cu) + abs(rv - ru)
                    m.wire_nm += length
                    name = LAYER_NAMES.get(lu, "")
                    if name == "M0":
                        m.m0_wire_nm += length
                    elif name == "BM0":
                        m.bm0_wire_nm += length
                else:
                    m.via_hops += 1
            elif mode == "tech":
                toks = line.split()
                if len(toks) >= 2:
                    key, val = toks[0], toks[1]
                    if key == "COL":
                        m.col = int(float(val))
                    elif key == "TRACK":
                        m.track = int(float(val))
                    elif key == "CPP":
                        m.cpp_nm = float(val)
                    elif key == "M0P":
                        m.m0p_nm = float(val)

    m.width_nm = m.cpp_nm * m.col
    m.height_nm = m.m0p_nm * m.track * 2
    m.area_nm2 = m.width_nm * m.height_nm
    return m


def parse_log(path: str, m: CellMetrics) -> None:
    with open(path) as f:
        for line in f:
            if "Objective function value:" in line:
                pass
            mobj = re.search(r"Obj#1.*result=(\d+)", line)
            if mobj:
                m.obj_cpp = int(mobj.group(1))
            mobj = re.search(r"Obj#4.*result=(\d+)", line)
            if mobj:
                m.obj_route = int(mobj.group(1))
            mobj = re.search(r"Elapsed time: ([\d.]+) seconds", line)
            if mobj:
                m.solve_s = float(mobj.group(1))


def format_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    keys = list(rows[0].keys())
    widths = {k: max(len(k), *(len(str(r[k])) for r in rows)) for k in keys}
    hdr = "  ".join(k.ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    body = "\n".join(
        "  ".join(str(r[k]).ljust(widths[k]) for k in keys) for r in rows
    )
    return f"{hdr}\n{sep}\n{body}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("res_files", nargs="+", help=".res result paths")
    p.add_argument("--log-dir", default=None, help="logs/ dir for solve time + obj breakdown")
    args = p.parse_args(argv)

    rows = []
    for res in args.res_files:
        m = parse_res(res)
        if args.log_dir:
            import os
            log = os.path.join(args.log_dir, f"{m.cell}.log")
            if os.path.isfile(log):
                parse_log(log, m)
        rows.append(m.to_row())

    print(format_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
