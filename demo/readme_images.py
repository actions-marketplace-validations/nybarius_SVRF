"""Generate the README images in docs/img/ from docs/BENCHMARK.md and docs/demo.cast.

    python3 demo/readme_images.py            # or: make images

Every number drawn comes from BENCHMARK.md (a sentence it cannot find is an error, so a
chart can never drift from the document); a test checks the committed files are fresh.
Charts carry their own light and dark colours (`prefers-color-scheme`); the banner and
the terminal are dark in both themes.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

PATTERNS = {
    "per_hour": r"([\d.]+) merged pull requests per hour",
    "before_wall": r"About ([\d.]+)-([\d.]+) minutes of wall time per pull request",
    "after_wall": r"About (\d+) seconds of wall time per merged pull request",
    "rounds": r"Across (\d+) live rounds: (\d+) pull requests merged using (\d+) gate runs, i\.e\. ([\d.]+) gates per",
    "best": r"Best batch: (\d+) pull requests landed from (\d+) gate runs in (\d+) minutes (\d+) seconds",
}


def benchmark_numbers(text: str) -> dict:
    flat = " ".join(text.split())
    found = {}
    for key, pattern in PATTERNS.items():
        hits = re.findall(pattern, flat)
        if not hits or (key == "per_hour" and len(hits) < 2):
            raise ValueError(f"BENCHMARK.md: no sentence matching {pattern!r}")
        found[key] = hits
    n = lambda s: float(s) if "." in s else int(s)  # noqa: E731
    rounds, best = found["rounds"][0], found["best"][0]
    return {"before_per_hour": n(found["per_hour"][0]), "after_per_hour": n(found["per_hour"][1]),
            "before_wall_min": n(found["before_wall"][0][0]), "before_wall_max": n(found["before_wall"][0][1]),
            "after_wall_seconds": n(found["after_wall"][0]), "gates_per_pr": n(rounds[3]), "rounds": n(rounds[0]),
            "merged": n(rounds[1]), "gate_runs": n(rounds[2]), "best_prs": n(best[0]), "best_gates": n(best[1]),
            "best_seconds": int(best[2]) * 60 + int(best[3])}


# ---- shared chart chrome

STYLE = f"""
.t{{font-family:{FONT};fill:#0b0b0b}} .s{{font-family:{FONT};fill:#52514e}} .m{{font-family:{FONT};fill:#7a7873}}
.card{{fill:#fcfcfb;stroke:rgba(11,11,11,.10)}} .grid{{stroke:#e7e6e0}} .base{{stroke:#c3c2b7}}
.before{{fill:#b9b8b0}} .after{{fill:#2a78d6}} .good{{fill:#0ca30c}} .accent{{fill:#2a78d6}}
.chipbg{{fill:rgba(42,120,214,.10)}} .chip{{font-family:{FONT};fill:#1c5cab;font-weight:650}}
@media (prefers-color-scheme: dark) {{
 .t{{fill:#f5f5f2}} .s{{fill:#c3c2b7}} .m{{fill:#8f8d86}} .card{{fill:#1a1a19;stroke:rgba(255,255,255,.10)}}
 .grid{{stroke:#2c2c2a}} .base{{stroke:#45453f}} .before{{fill:#5b5a55}} .after{{fill:#3987e5}}
 .accent{{fill:#3987e5}} .chipbg{{fill:rgba(57,135,229,.18)}} .chip{{fill:#86b6ef}}
}}"""


def svg(width: int, height: int, label: str, body: list[str], style: str = STYLE) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{escape(label)}">',
        f"<title>{escape(label)}</title>",
        f"<style>{style}</style>",
        *body,
        "</svg>",
        "",
    ])


def column(x: float, base: float, w: float, h: float, cls: str, r: float = 6) -> str:
    """A column with rounded top corners, square at the baseline."""
    r = min(r, w / 2, h)
    top = base - h
    return (f'<path class="{cls}" d="M{x:.1f},{base:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}'
            f'H{x + w - r:.1f}Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f}V{base:.1f}Z"/>')


def pair_panel(x0: float, y0: float, w: float, h: float, title: str, subtitle: str, before: tuple, after: tuple,
               top: float, callout: str) -> list[str]:
    """One card: a before column and an after column on a shared zero baseline."""
    out = [f'<rect class="card" x="{x0 + .5}" y="{y0 + .5}" width="{w - 1}" height="{h - 1}" rx="14"/>',
           f'<text class="t" x="{x0 + 22}" y="{y0 + 36}" font-size="17" font-weight="650">{escape(title)}</text>',
           f'<text class="m" x="{x0 + 22}" y="{y0 + 58}" font-size="13">{escape(subtitle)}</text>']
    base = y0 + h - 44
    plot_top = y0 + 136
    ih = base - plot_top
    cw = 64
    gap = (w - 44 - 2 * cw) / 3
    for i, (label, value, text, cls) in enumerate((before + ("before",), after + ("after",))):
        x = x0 + 22 + gap + i * (cw + gap)
        ch = max(3, ih * value / top)
        out.append(column(x, base, cw, ch, cls))
        out.append(f'<text class="t" x="{x + cw / 2}" y="{base - ch - 10}" font-size="17" font-weight="650" '
                   f'text-anchor="middle">{escape(text)}</text>')
        out.append(f'<text class="s" x="{x + cw / 2}" y="{base + 24}" font-size="13" text-anchor="middle">'
                   f'{escape(label)}</text>')
    out.append(f'<line class="base" x1="{x0 + 22}" x2="{x0 + w - 22}" y1="{base}" y2="{base}"/>')
    cw2 = 16 + 7.6 * len(callout)
    out.append(f'<rect class="chipbg" x="{x0 + w - 22 - cw2}" y="{y0 + 72}" width="{cw2:.0f}" height="26" rx="13"/>')
    out.append(f'<text class="chip" x="{x0 + w - 22 - cw2 / 2}" y="{y0 + 90}" font-size="13" '
               f'text-anchor="middle">{escape(callout)}</text>')
    return out


def fmt_seconds(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}"


def throughput(b: dict, x0=0, y0=0) -> list[str]:
    ratio = b["after_per_hour"] / b["before_per_hour"]
    return pair_panel(x0, y0, 360, 330, "Merges per hour", "same machine, same repository",
                      ("before", b["before_per_hour"], f'{b["before_per_hour"]}'),
                      ("with SVRF", b["after_per_hour"], f'{b["after_per_hour"]}'), 18, f"{ratio:.1f}×")


def wall_time(b: dict, x0=0, y0=0) -> list[str]:
    before = (b["before_wall_min"] + b["before_wall_max"]) / 2 * 60
    return pair_panel(x0, y0, 360, 330, "Wall time per PR", "lower is better",
                      ("before", before, f'~{before / 60:.0f} min'),
                      ("with SVRF", b["after_wall_seconds"], f'~{b["after_wall_seconds"]} s'), 200,
                      f"{b['before_wall_min']:g}–{b['before_wall_max']:g} min → {b['after_wall_seconds']} s")


def gates(b: dict, x0=0, y0=0) -> list[str]:
    return pair_panel(x0, y0, 360, 330, "Gate runs per merged PR", "bisection of red batches included",
                      ("one by one", 1.0, "1.00"), ("with SVRF", b["gates_per_pr"], f'{b["gates_per_pr"]:.2f}'), 1.15,
                      f'{b["merged"]} PRs, {b["gate_runs"]} gates')


def best_strip(b: dict, x0: float, y0: float, w: float, mismatches: int) -> list[str]:
    out = [f'<rect class="card" x="{x0 + .5}" y="{y0 + .5}" width="{w - 1}" height="115" rx="14"/>',
           f'<text class="m" x="{x0 + 22}" y="{y0 + 30}" font-size="13">Best batch of the day</text>']
    stats = [(str(b["best_prs"]), "pull requests landed"), (str(b["best_gates"]), "gate runs"),
             (fmt_seconds(b["best_seconds"]), "minutes, end to end"), (str(mismatches), "landed-tree mismatches")]
    col = (w - 44) / len(stats)
    for i, (value, label) in enumerate(stats):
        x = x0 + 22 + i * col
        out.append(f'<text class="t" x="{x}" y="{y0 + 70}" font-size="32" font-weight="700" '
                   f'letter-spacing="-0.5">{escape(value)}</text>')
        out.append(f'<text class="s" x="{x}" y="{y0 + 94}" font-size="14">{escape(label)}</text>')
    return out


def mismatches(text: str) -> int:
    found = re.search(r"(\d+) landed-tree mismatches", " ".join(text.split()))
    if not found:
        raise ValueError("BENCHMARK.md: no landed-tree mismatch count")
    return int(found.group(1))


def benchmark(b: dict, mismatch_count: int) -> str:
    body = throughput(b, 0, 0) + wall_time(b, 380, 0) + gates(b, 760, 0) + best_strip(b, 0, 350, 1120, mismatch_count)
    return svg(1120, 466, "SVRF benchmark: merges per hour, wall time per PR, gates per PR", body)


# ---- architecture

def architecture() -> str:
    style = STYLE + f"""
.box{{fill:#fcfcfb;stroke:#c3c2b7;stroke-width:1.2}} .hot{{fill:rgba(42,120,214,.08);stroke:#2a78d6;stroke-width:1.4}}
.red{{fill:rgba(208,59,59,.07);stroke:#d03b3b;stroke-width:1.2}} .ok{{fill:rgba(12,163,12,.08);stroke:#0ca30c;stroke-width:1.4}}
.edge{{stroke:#898781;stroke-width:1.6;fill:none}} .edge.r{{stroke:#d03b3b}} .edge.g{{stroke:#0ca30c}}
.head{{fill:#898781}} .head.r{{fill:#d03b3b}} .head.g{{fill:#0ca30c}} .pr{{fill:#fcfcfb;stroke:#c3c2b7}}
.prline{{stroke:#c3c2b7;stroke-width:2;stroke-linecap:round}} .rtext{{font-family:{FONT};fill:#b83232}}
.gtext{{font-family:{FONT};fill:#087a08}}
@media (prefers-color-scheme: dark) {{
 .box{{fill:#1a1a19;stroke:#45453f}} .hot{{fill:rgba(57,135,229,.12);stroke:#3987e5}} .red{{fill:rgba(208,59,59,.12)}}
 .ok{{fill:rgba(12,163,12,.12)}} .pr{{fill:#1a1a19;stroke:#45453f}} .prline{{stroke:#45453f}}
 .rtext{{fill:#ef6b6b}} .gtext{{fill:#3cc03c}}
}}"""
    W, H = 1200, 320
    y, h = 30, 120
    boxes = [  # x, width, class, title, lines
        (150, 176, "box", "Admission", ["merges onto main?", "history in order?", "drafts, forks, holds out"]),
        (356, 190, "box", "Batch planner", ["pairwise conflict read", "largest compatible set", "families of 8, stacked"]),
        (576, 170, "hot", "Gate slots", []),
        (776, 190, "ok", "Landing", ["merge commit pinned", "to the gated sha", "fetch, compare trees"]),
        (996, 180, "ok", "main", []),
    ]
    out = []
    # the swarm: a stack of pull-request cards
    for i in range(4):
        cx, cy = 18 + i * 7, y + 8 + i * 9
        out.append(f'<rect class="pr" x="{cx}" y="{cy}" width="92" height="64" rx="8"/>')
    for k in range(3):
        out.append(f'<line class="prline" x1="{48}" x2="{104 - k * 14}" y1="{y + 50 + k * 12}" y2="{y + 50 + k * 12}"/>')
    out.append(f'<circle class="accent" cx="{50 - 13}" cy="{y + 50}" r="5"/>')
    out.append(f'<text class="t" x="{70}" y="{y + h + 32}" font-size="15" font-weight="650" text-anchor="middle">PR swarm</text>')
    out.append(f'<text class="m" x="{70}" y="{y + h + 52}" font-size="12.5" text-anchor="middle">agents open PRs</text>')

    def arrow(x1, y1, x2, y2, cls=""):
        return (f'<path class="edge {cls}" d="M{x1},{y1} L{x2 - 7},{y2}"/>'
                f'<path class="head {cls}" d="M{x2},{y2} l-9,-5 v10 z"/>')

    out.append(arrow(124, y + h / 2, 150, y + h / 2))
    for x, w, cls, title, lines in boxes:
        out.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="14"/>')
        out.append(f'<text class="t" x="{x + w / 2}" y="{y + 30}" font-size="16" font-weight="650" '
                   f'text-anchor="middle">{escape(title)}</text>')
        for i, line in enumerate(lines):
            out.append(f'<text class="s" x="{x + w / 2}" y="{y + 56 + i * 20}" font-size="13" '
                       f'text-anchor="middle">{escape(line)}</text>')
    for (x, w, *_), (nx, *_) in zip(boxes, boxes[1:]):
        out.append(arrow(x + w, y + h / 2, nx, y + h / 2, "g" if nx == 996 else ""))
    # gate slots: two parallel lanes
    for i, label in enumerate(("slot 1  F1", "slot 2  F2 on F1")):
        sy = y + 44 + i * 36
        out.append(f'<rect class="box" x="{591}" y="{sy}" width="140" height="26" rx="7"/>')
        out.append(f'<text class="s" x="{606}" y="{sy + 17.5}" font-size="12.5">{escape(label)}</text>')
    # main: a branch line with merge dots and a check
    out.append(f'<line class="edge g" x1="1016" x2="1156" y1="{y + 58}" y2="{y + 58}"/>')
    for i in range(4):
        out.append(f'<circle class="good" cx="{1030 + i * 38}" cy="{y + 58}" r="6"/>')
    out.append(f'<text class="gtext" x="1086" y="{y + 88}" font-size="12.5" text-anchor="middle" '
               f'font-weight="600">\u2713 every landed tree</text>')
    out.append(f'<text class="gtext" x="1086" y="{y + 105}" font-size="12.5" text-anchor="middle" '
               f'font-weight="600">= its gated tree</text>')
    # bisection loop under the gate slots
    by = y + h + 70
    out.append(f'<rect class="red" x="576" y="{by}" width="170" height="74" rx="14"/>')
    out.append(f'<text class="t" x="661" y="{by + 28}" font-size="16" font-weight="650" text-anchor="middle">Bisection</text>')
    out.append(f'<text class="s" x="661" y="{by + 50}" font-size="13" text-anchor="middle">red batch → two halves</text>')
    out.append(f'<text class="s" x="661" y="{by + 66}" font-size="13" text-anchor="middle">regated on current main</text>')
    out.append(f'<path class="edge r" d="M631,{y + h} V{by - 7}"/><path class="head r" d="M631,{by} l-5,-9 h10 z"/>')
    out.append(f'<text class="rtext" x="621" y="{y + h + 38}" font-size="12.5" text-anchor="end" font-weight="600">red</text>')
    out.append(f'<path class="edge" d="M691,{by} V{y + h + 7}"/><path class="head" d="M691,{y + h} l-5,9 h10 z"/>')
    out.append(f'<text class="m" x="701" y="{y + h + 38}" font-size="12.5">halves</text>')
    out.append(f'<text class="m" x="766" y="{by + 32}" font-size="12.5">a single red PR is held</text>')
    out.append(f'<text class="m" x="766" y="{by + 50}" font-size="12.5">with its failing lines</text>')
    out.append(f'<text class="m" x="20" y="{by + 44}" font-size="12.5">One pull-request list per round; a failed read is retried, never a hold.</text>')
    return svg(W, H, "SVRF architecture: PR swarm, admission, batch planner, gate slots, bisection, landing with tree check",
               out, style)


# ---- the comment the train leaves on a merged pull request (the format of Train.land_family)

def pr_comment() -> str:
    W, H = 760, 196
    family, prs, tree = "F1", "[141, 142, 143, 144, 145, 146, 147, 148]", "3f9c2a7be410"
    receipt = "train-20260925T170505Z-4242-0137.json"
    text = (f"Merged by SVRF in family {family} {prs}, gated once on tree {tree}; the landed tree {tree} "
            f"was checked against the planned tree. Receipt: {receipt}.")
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > 84:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    out = [f'<rect class="card" x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="12"/>',
           f'<path class="chipbg" d="M.5,12.5 a12,12 0 0 1 12,-12 H{W - 12.5} a12,12 0 0 1 12,12 V48 H.5 Z"/>',
           '<rect x="18" y="12" width="24" height="24" rx="7" class="accent"/>',
           '<path d="M24.5 31 V28 Q24.5 25.5 30 23.5 Q35.5 25.5 35.5 28 V31 M30 23.5 V16.5 M27 19.5 L30 16.5 L33 19.5" '
           'fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
           '<text class="t" x="52" y="29" font-size="14" font-weight="650">svrf</text>',
           '<text class="m" x="88" y="29" font-size="14">commented on a merged pull request</text>']
    for i, row in enumerate(lines):
        out.append(f'<text class="s" x="20" y="{80 + i * 22}" font-size="14">{escape(row)}</text>')
    out.append('<text class="m" x="20" y="{}" font-size="12.5">\u2713 gated once \u00b7 landed tree compared with '
               'the gated tree \u00b7 receipt on disk</text>'.format(80 + len(lines) * 22 + 14))
    return svg(W, H, "The comment SVRF leaves on a merged pull request", out)


# ---- banner

def hero() -> str:
    W, H = 1280, 320
    style = f"""
.hx{{font-family:{FONT}}} .lane{{fill:none;stroke-width:3;stroke-linecap:round}}
"""
    colors = ["#3987e5", "#6da7ec", "#1baf7a", "#86b6ef", "#9085e9", "#3987e5"]
    out = ['<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#0d1524"/><stop offset="1" stop-color="#101010"/></linearGradient>'
           '<radialGradient id="glow" cx="0.78" cy="0.5" r="0.5"><stop offset="0" stop-color="#2a78d6" stop-opacity=".35"/>'
           '<stop offset="1" stop-color="#2a78d6" stop-opacity="0"/></radialGradient></defs>',
           f'<rect width="{W}" height="{H}" rx="18" fill="url(#bg)"/>',
           f'<rect width="{W}" height="{H}" rx="18" fill="url(#glow)"/>']
    # braided lanes: six pull requests converging into two families, then one landed line
    x0, xm, x1, xe = 870, 1010, 1095, 1250
    for i, c in enumerate(colors):
        sy = 60 + i * 40
        fy = 130 if i < 3 else 190
        out.append(f'<path class="lane" stroke="{c}" stroke-opacity=".9" d="M{x0},{sy} C{x0 + 80},{sy} {xm - 70},{fy} {xm},{fy}"/>')
        out.append(f'<circle cx="{x0}" cy="{sy}" r="6" fill="{c}"/>')
    for fy in (130, 190):
        out.append(f'<path class="lane" stroke="#e8eef8" d="M{xm},{fy} C{xm + 45},{fy} {x1 - 45},160 {x1},160"/>')
        out.append(f'<rect x="{xm - 22}" y="{fy - 14}" width="44" height="28" rx="8" fill="#16243a" stroke="#6da7ec"/>')
    out.append(f'<text class="hx" x="{xm}" y="135" font-size="12" fill="#cde2fb" text-anchor="middle" font-weight="600">F1</text>')
    out.append(f'<text class="hx" x="{xm}" y="195" font-size="12" fill="#cde2fb" text-anchor="middle" font-weight="600">F2</text>')
    out.append(f'<path class="lane" stroke="#0ca30c" stroke-width="4" d="M{x1},160 H{xe}"/>')
    for i in range(3):
        out.append(f'<circle cx="{x1 + 42 + i * 44}" cy="160" r="8" fill="#0ca30c" stroke="#0f1a12" stroke-width="3"/>')
    out.append(f'<circle cx="{x1}" cy="160" r="15" fill="#0f1a12" stroke="#0ca30c" stroke-width="2.5"/>')
    out.append(f'<path d="M{x1 - 6},160 l4,5 l8,-10" fill="none" stroke="#0ca30c" stroke-width="2.6" '
               f'stroke-linecap="round" stroke-linejoin="round"/>')
    out.append(f'<text class="hx" x="{x1 + 80}" y="200" font-size="13" fill="#8fd18f" text-anchor="middle">'
               f'landed = gated</text>')
    # words
    out.append('<rect x="56" y="62" width="46" height="46" rx="12" fill="#2a78d6"/>')
    out.append('<path d="M68.5 99 V92.5 Q68.5 87 79 83 Q89.5 87 89.5 92.5 V99 M79 83 V70 M73 76 L79 70 L85 76" '
               'fill="none" stroke="#fff" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/>')
    out.append('<text class="hx" x="118" y="96" font-size="30" font-weight="750" fill="#ffffff" letter-spacing="-0.5">SVRF</text>')
    out.append('<text class="hx" x="56" y="160" font-size="34" font-weight="700" fill="#ffffff" letter-spacing="-0.8">'
               'The merge queue built for agent swarms</text>')
    out.append('<text class="hx" x="56" y="200" font-size="19" fill="#b7c4d8">Batches, bisects, and verifies each landing.</text>')
    for i, word in enumerate(("batch", "gate once", "bisect red", "land + verify")):
        x = 56 + i * 128
        out.append(f'<rect x="{x}" y="236" width="{116}" height="30" rx="15" fill="#ffffff" fill-opacity=".07" '
                   f'stroke="#ffffff" stroke-opacity=".14"/>')
        out.append(f'<text class="hx" x="{x + 58}" y="256" font-size="13.5" fill="#dfe7f3" text-anchor="middle">{word}</text>')
    return svg(W, H, "SVRF: the merge queue built for agent swarms. Batches, bisects, and verifies each landing.",
               out, style)


def main(out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir or ROOT / "docs" / "img")
    out_dir.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
    b = benchmark_numbers(text)
    spec = importlib.util.spec_from_file_location("cast_to_svg", ROOT / "demo" / "cast_to_svg.py")
    cast = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cast)
    files = {
        "hero.svg": hero(),
        "benchmark.svg": benchmark(b, mismatches(text)),
        "throughput.svg": svg(360, 330, "Merges per hour, before and with SVRF", throughput(b)),
        "wall-time.svg": svg(360, 330, "Wall time per merged PR, before and with SVRF", wall_time(b)),
        "gates-per-pr.svg": svg(360, 330, "Gate runs per merged PR", gates(b)),
        "architecture.svg": architecture(),
        "pr-comment.svg": pr_comment(),
        "demo.svg": cast.render((ROOT / "docs" / "demo.cast").read_text(encoding="utf-8")),
    }
    written = []
    for name, text in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in main(Path(sys.argv[1]) if len(sys.argv) > 1 else None):
        print(p)
