"""Generate the Retchat app icon: a speech bubble shaped like a ratchet wheel."""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "org.selfmade.Retchat"
COLOR_OUT = os.path.join(HERE, "hicolor", "scalable", "apps", f"{APP_ID}.svg")
SYMBOLIC_OUT = os.path.join(HERE, "hicolor", "symbolic", "apps", f"{APP_ID}-symbolic.svg")

# Colours of the full-colour icon. The face gradient runs diagonally from
# top left to bottom right (lilac to coral pink, as in the Columba icon).
PALETTE = {
    "face_start": "#c471ed",   # toothed rim, top left
    "face_end": "#f64f59",     # toothed rim, bottom right
    "depth": "#a2356f",        # bottom edge (GNOME style depth)
    "shadow": "#a2356f",       # shadow of the inner bubble face
    "dots": "#d6589f",         # mesh / typing dots
    "paper_start": "#ffffff",  # inner bubble face
    "paper_end": "#deddda",
}


def ratchet_bubble_path(cx, cy, r_tip, r_root, teeth, tail_tip, tail_a0, tail_a1, prec=2, rot=-90.0):
    """Outline of a sawtooth ratchet wheel merged with a speech bubble tail.

    Angles in degrees, clockwise from 12 o'clock (SVG y-down). Each tooth rises
    gradually from the root to the tip and drops radially (classic ratchet).
    The teeth between tail_a0 and tail_a1 are replaced by the tail (the pawl).
    """
    step = 360.0 / teeth
    fmt = f"{{:.{prec}f}}"
    f = fmt.format

    def pt(r, deg):
        a = math.radians(deg + rot)
        return f(cx + r * math.cos(a)), f(cy + r * math.sin(a))

    pts = []
    tail_done = False
    for i in range(teeth):
        a0 = i * step
        a1 = a0 + step
        if tail_a0 <= a0 < tail_a1 or tail_a0 < a1 <= tail_a1:
            if not tail_done:
                pts.append(pt(r_root, tail_a0))
                pts.append((f(tail_tip[0]), f(tail_tip[1])))
                pts.append(pt(r_root, tail_a1))
                tail_done = True
            continue
        pts.append(pt(r_root, a0))          # tooth base
        pts.append(pt(r_tip, a0 + step * 0.92))  # rising flank up to the tip
        pts.append(pt(r_root, a1 - 0.01 * step))  # radial drop
    d = "M " + " L ".join(f"{x},{y}" for x, y in pts) + " Z"
    return d


# ---------------------------------------------------------------- full colour
CX, CY = 64, 58
R_TIP, R_ROOT, TEETH = 51, 45, 16
TAIL = (22, 111)          # tip of the bubble tail / pawl
TAIL_A = (210, 240)       # angular range replaced by the tail (clockwise from 12 o'clock)

shape = ratchet_bubble_path(CX, CY, R_TIP, R_ROOT, TEETH, TAIL, *TAIL_A)


def _polar(r, deg):
    a = math.radians(deg - 90)
    return CX + r * math.cos(a), CY + r * math.sin(a)


_t0, _t1 = _polar(R_ROOT, TAIL_A[0]), _polar(R_ROOT, TAIL_A[1])
tail_side = f"M {_t0[0]:.2f},{_t0[1]:.2f} L {TAIL[0]},{TAIL[1]} L {_t1[0]:.2f},{_t1[1]:.2f} Z"

FACE_R = 35
dots = [(45, 58), (64, 58), (83, 58)]
dot_r = 6.5

def build_color_svg(c: dict) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <title>Retchat</title>
  <defs>
    <linearGradient id="face" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{c['face_start']}"/>
      <stop offset="1" stop-color="{c['face_end']}"/>
    </linearGradient>
    <linearGradient id="paper" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{c['paper_start']}"/>
      <stop offset="1" stop-color="{c['paper_end']}"/>
    </linearGradient>
  </defs>
  <!-- side / depth (GNOME style bottom edge) -->
  <g transform="translate(0 6)" fill="{c['depth']}" stroke="{c['depth']}" stroke-width="2" stroke-linejoin="round">
    <circle cx="{CX}" cy="{CY}" r="{R_TIP - 3}"/>
    <path d="{tail_side}"/>
  </g>
  <!-- ratchet wheel speech bubble -->
  <path d="{shape}" fill="url(#face)" stroke="url(#face)" stroke-width="2" stroke-linejoin="round"/>
  <!-- bubble face inside the toothed rim -->
  <circle cx="{CX}" cy="{CY + 2.5}" r="{FACE_R}" fill="{c['shadow']}" fill-opacity="0.45"/>
  <circle cx="{CX}" cy="{CY}" r="{FACE_R}" fill="url(#paper)"/>
  <!-- mesh / typing dots -->
  <path d="M {dots[0][0]},{dots[0][1]} L {dots[2][0]},{dots[2][1]}" stroke="{c['dots']}" stroke-width="5" stroke-linecap="round"/>
  <g fill="{c['dots']}">
{chr(10).join(f'    <circle cx="{x}" cy="{y}" r="{dot_r}"/>' for x, y in dots)}
  </g>
</svg>
"""

# ---------------------------------------------------------------- symbolic 16px
s_shape = ratchet_bubble_path(8, 7.2, 6.9, 5.9, 12, (1.8, 15.0), 205, 240, prec=3)
s_dots = [(5.6, 7.2), (8, 7.2), (10.4, 7.2)]
s_r = 0.95
s_face = 4.5


def circle_path(x, y, r):
    return (f"M {x - r:.3f},{y:.3f} a {r},{r} 0 1 0 {2 * r:.3f},0 "
            f"a {r},{r} 0 1 0 {-2 * r:.3f},0 Z")


symbolic_svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <path fill="#222222" fill-rule="evenodd" d="{s_shape} {circle_path(8, 7.2, s_face)}"/>
  <path fill="#222222" d="{' '.join(circle_path(x, y, s_r) for x, y in s_dots)}"/>
</svg>
"""

if __name__ == "__main__":
    with open(COLOR_OUT, "w") as f:
        f.write(build_color_svg(PALETTE))
    with open(SYMBOLIC_OUT, "w") as f:
        f.write(symbolic_svg)
    print("written", COLOR_OUT, SYMBOLIC_OUT)
