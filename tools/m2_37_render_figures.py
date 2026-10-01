"""Render dependency-free SVG figures from completed M2.37 summaries."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/dmv-m2-37-performance-scaling"


def svg(path: Path, title: str, x_label: str, y_label: str, series: list[tuple[str, list[tuple[float, float]]]]) -> None:
    width, height, left, bottom = 900, 520, 82, 70
    xs = [x for _name, points in series for x, _y in points]
    ys = [y for _name, points in series for _x, y in points]
    xmax = max(xs) if xs else 1.0
    ymax = max(ys) * 1.1 if ys else 1.0
    colors = ("#1b5e20", "#1565c0", "#ef6c00", "#6a1b9a", "#00838f")
    def point(x: float, y: float) -> tuple[float, float]:
        return left + (x / xmax) * (width - left - 30), height - bottom - (y / ymax) * (height - bottom - 35)
    content = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
               f'<text x="{width/2}" y="25" text-anchor="middle" font-size="18">{title}</text>',
               f'<line x1="{left}" y1="35" x2="{left}" y2="{height-bottom}" stroke="black"/>',
               f'<line x1="{left}" y1="{height-bottom}" x2="{width-30}" y2="{height-bottom}" stroke="black"/>']
    for index, (name, values) in enumerate(series):
        coords = " ".join(f"{point(x,y)[0]:.1f},{point(x,y)[1]:.1f}" for x, y in values)
        color = colors[index % len(colors)]
        content.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in values:
            px, py = point(x, y)
            content.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5" fill="{color}"/>')
        lx, ly = point(values[-1][0], values[-1][1])
        content.append(f'<text x="{lx+6:.1f}" y="{ly:.1f}" font-size="12">{name}</text>')
    content.extend([f'<text x="{width/2}" y="{height-18}" text-anchor="middle">{x_label}</text>',
                    f'<text x="18" y="{height/2}" transform="rotate(-90 18 {height/2})" text-anchor="middle">{y_label}</text>', '</svg>'])
    path.write_text("\n".join(content) + "\n")


def main() -> int:
    b = json.loads((OUT / "repository-scaling/summary.json").read_text())
    br = b["rows"]
    bdir = OUT / "repository-scaling/figures"; bdir.mkdir(parents=True, exist_ok=True)
    svg(bdir / "B1-acquisition-wall-clock.svg", "M2.37 B1 repository acquisition", "candidate states (C)", "seconds", [("acquisition", [(r["candidate_count"], r["total_s"]["median"]) for r in br])])
    svg(bdir / "B2-repository-bytes.svg", "M2.37 B2 repository size", "candidate states (C)", "bytes", [("repository bytes", [(r["candidate_count"], r["repository_bytes"]["median"]) for r in br])])
    c = json.loads((OUT / "input-scaling/summary.json").read_text())
    cr = c["rows"]
    cdir = OUT / "input-scaling/figures"; cdir.mkdir(parents=True, exist_ok=True)
    svg(cdir / "C1-materialization-vs-N.svg", "M2.37 C1 physical materialization", "input rows (N)", "seconds", [("physical materialization", [(r["input_rows"], r["physical_materialization_s"]["median"]) for r in cr])])
    svg(cdir / "C2-activation-vs-N.svg", "M2.37 C2 hypothetical activation", "input rows (N)", "seconds", [("activation", [(r["input_rows"], r["hypothetical_activation_s"]["median"]) for r in cr])])
    svg(cdir / "C3-total-vs-N.svg", "M2.37 C3 per-design totals", "input rows (N)", "seconds", [("physical", [(r["input_rows"], r["physical_total_s"]["median"]) for r in cr]), ("hypothetical", [(r["input_rows"], r["hypothetical_total_s"]["median"]) for r in cr])])
    svg(cdir / "C4-ratio-vs-N.svg", "M2.37 C4 total ratio", "input rows (N)", "physical / hypothetical", [("ratio", [(r["input_rows"], r["ratio"]) for r in cr])])
    svg(cdir / "C5-removable-fraction-vs-N.svg", "M2.37 C5 removable materialization fraction", "input rows (N)", "fraction", [("fraction", [(r["input_rows"], r["removable_materialization_fraction"]) for r in cr])])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
