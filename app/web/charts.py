"""SVG chart geometry, precomputed in Python (no client-side charting library).

Templates just loop over the returned dicts and render plain SVG - all the
trigonometry (gauge needle/arc, donut ring segments) happens here once per
request instead of being fought out in Jinja.
"""
import math


def donut(segments: list[tuple], size: int = 132, stroke: int = 15) -> dict:
    """segments: [(value, css_class), ...]. Returns ring arcs (stroke-dasharray/offset)."""
    total = sum(v for v, _ in segments)
    r = (size - stroke) / 2
    circumference = 2 * math.pi * r
    arcs = []
    if total <= 0:
        arcs.append({"cls": "ring-empty", "dasharray": f"{circumference:.2f} 0", "dashoffset": "0"})
    else:
        offset = 0.0
        for value, cls in segments:
            if value <= 0:
                continue
            dash = (value / total) * circumference
            arcs.append({
                "cls": cls,
                "dasharray": f"{dash:.2f} {circumference - dash:.2f}",
                "dashoffset": f"{-offset:.2f}",
            })
            offset += dash
    return {"size": size, "stroke": stroke, "r": round(r, 2), "c": size / 2, "arcs": arcs, "total": total}


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple:
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def gauge(pct: float, size: int = 224, stroke: int = 16) -> dict:
    """Semicircular gauge (opens at the bottom) with a needle. pct in [0, 100]."""
    pct = max(0.0, min(100.0, pct))
    height = round(size * 0.6)
    cx, r = size / 2, (size - stroke) / 2 - 2
    cy = height - stroke / 2 - 4

    def arc_path(t_end: float) -> str:
        a0, a1 = 180.0, 180.0 + 180.0 * t_end
        x0, y0 = _polar(cx, cy, r, a0)
        x1, y1 = _polar(cx, cy, r, a1)
        large = 1 if (a1 - a0) > 180 else 0
        return f"M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}"

    needle_angle = 180.0 + 180.0 * (pct / 100.0)
    nx, ny = _polar(cx, cy, r - stroke * 1.4, needle_angle)

    return {
        "size": size, "height": height, "cx": round(cx, 2), "cy": round(cy, 2), "r": round(r, 2),
        "bg_path": arc_path(1.0), "value_path": arc_path(pct / 100.0),
        "needle_x": round(nx, 2), "needle_y": round(ny, 2), "pct": round(pct),
    }


def sparkline(values: list, width: int = 128, height: int = 46, gap: float = 5.0) -> dict:
    """Simple vertical bar sparkline; values already in display order (oldest -> newest)."""
    n = max(1, len(values))
    bw = (width - gap * (n - 1)) / n
    maxv = max(values) if values and max(values) > 0 else 1
    bars = []
    for i, v in enumerate(values):
        bh = round(max(3, (v / maxv) * height), 2)
        bars.append({
            "x": round(i * (bw + gap), 2), "y": round(height - bh, 2),
            "w": round(bw, 2), "h": bh, "v": v,
        })
    return {"bars": bars, "width": width, "height": height}
