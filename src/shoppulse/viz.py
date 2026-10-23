"""Shared chart styling: one palette for the static report AND the dashboard.

Categorical hues are used in a FIXED order (never cycled) and text always uses
ink colours, not series colours. Values come from a validated palette.
"""

from __future__ import annotations

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e7e6e1"

# categorical slots 1-4: blue, orange, aqua, yellow
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
SERIES = [BLUE, ORANGE, AQUA, YELLOW]
SEQ_LOW, SEQ_HIGH = "#e4eefb", "#174a91"  # one hue, light -> dark
MUTED_LINE = "#9ab9e6"


def apply_matplotlib_style() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titlesize": 13, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
        "font.size": 10, "legend.frameon": False, "lines.linewidth": 2,
    })


def plotly_layout(title: str, height: int = 340) -> dict:
    return dict(
        title=dict(text=title, x=0, font=dict(size=15, color=INK)),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, height=height,
        margin=dict(l=48, r=16, t=48, b=40),
        font=dict(color=INK_2, size=12),
        xaxis=dict(gridcolor=GRID, linecolor=GRID, zeroline=False),
        yaxis=dict(gridcolor=GRID, linecolor=GRID, zeroline=False),
        legend=dict(orientation="h", y=-0.2),
        hovermode="x unified",
    )
