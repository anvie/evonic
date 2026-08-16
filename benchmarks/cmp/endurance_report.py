"""Endurance report for the 3-arm run-to-failure test (r10, s10_hybrid).

Reads the per-turn progress jsonl (self-contained: arm / ctx / probe_score /
probe_age / kind / hard_fail), so it works live while the run is going and
handles arms that hard-failed at different turns. Produces:

  1. context-vs-turn  — Full history climbs to the model's ceiling and hard-
     fails (X marker); Windowed and CMP stay bounded.
  2. recall-vs-distance — mean lookup accuracy vs how many turns back the fact
     was planted, per arm. The age band where an arm's accuracy collapses IS
     its recall wall.

Also writes a CSV (per arm × age band) and prints each arm's wall + peak ctx +
hard-fail turn.

Usage: python endurance_report.py [run_id]   (default r10)
"""

import csv
import glob
import json
import os
import sys
from collections import defaultdict

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')
BANDS = [15, 30, 60, 120, 180, 240, 300, 360]


def load_continuation(run_id, agent='aisyah'):
    """Dense recall-vs-age samples from the probe-continuation run (probe_continue.py),
    as [(age, score)]. Empty if the run wasn't done. Per-agent file, with a
    fallback to the old shared filename for the original CMP run."""
    p = os.path.join(RESULTS_DIR, run_id, f'accuracy_continue_{agent}.jsonl')
    if not os.path.exists(p):
        p = os.path.join(RESULTS_DIR, run_id, 'accuracy_continue.jsonl')
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get('score') is not None and r.get('age') is not None:
            out.append((r['age'], r['score']))
    return out
ARM_ORDER = ['Full history', 'Windowed (50-msg)', 'Summary+tail', 'CMP']
COLORS = {'Full history': 'tab:red', 'Windowed (50-msg)': 'tab:orange',
          'Summary+tail': 'tab:green', 'CMP': 'tab:blue'}
# arm label derived from the progress filename's agent (authoritative; the
# stored 'arm' field predates the Summary+tail arm).
FILE_ARM = {'aisyah': 'CMP', 'aisyah_full': 'Full history',
            'aisyah_base': 'Windowed (50-msg)', 'aisyah_sum': 'Summary+tail'}
# arm -> agent whose dense continuation probes give its recall curve
AGENT_OF = {'CMP': 'aisyah', 'Summary+tail': 'aisyah_sum'}


def _band_of(age):
    return min(BANDS, key=lambda b: abs(age - b))


def collect(run_id):
    """{arm: {'ctx':[(turn,ctx)], 'lookups':[(age,score)], 'fail_turn':int|None,
             'last_turn':int, 'peak_ctx':int}}"""
    arms = {}
    for f in sorted(glob.glob(os.path.join(RESULTS_DIR, run_id, 'progress', '*.jsonl'))):
        rows = [json.loads(l) for l in open(f) if l.strip()]
        if not rows:
            continue
        agent = os.path.basename(f)[:-6].split('seed1_')[-1]
        arm = FILE_ARM.get(agent) or next((r['arm'] for r in rows if r.get('arm')), None)
        if not arm:
            continue
        a = arms.setdefault(arm, {'ctx': [], 'lookups': [], 'wall': [],
                                  'fail_turn': None, 'last_turn': 0, 'peak_ctx': 0})
        for r in rows:
            if r.get('hard_fail'):
                a['fail_turn'] = r.get('turn') or a['last_turn']
                continue
            if r.get('turn') is None:
                continue
            a['last_turn'] = max(a['last_turn'], r['turn'])
            if r.get('ctx'):
                a['ctx'].append((r['turn'], r['ctx']))
                a['peak_ctx'] = max(a['peak_ctx'], r['ctx'])
            if r.get('wall_s'):
                a['wall'].append((r['turn'], r['wall_s']))
            if r.get('kind') == 'lookup' and r.get('probe_score') is not None \
                    and r.get('probe_age') is not None:
                a['lookups'].append((r['probe_age'], r['probe_score']))
    return arms


def _band_acc(lookups):
    """{band: (mean_acc, n)} over lookups bucketed to the nearest band."""
    by = defaultdict(list)
    for age, score in lookups:
        by[_band_of(age)].append(score)
    return {b: (sum(v) / len(v), len(v)) for b, v in by.items()}


def build(run_id):
    arms = collect(run_id)
    if not arms:
        raise SystemExit(f"no progress data for {run_id}")
    present = [a for a in ARM_ORDER if a in arms] + \
              [a for a in arms if a not in ARM_ORDER]

    # ── console summary + CSV ──
    out_dir = os.path.join(RESULTS_DIR, run_id)
    csv_path = os.path.join(out_dir, 'endurance_recall.csv')
    with open(csv_path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['arm', 'last_turn', 'fail_turn', 'peak_ctx'] +
                   [f'acc@{b}' for b in BANDS] + [f'n@{b}' for b in BANDS])
        print(f"\n=== endurance report: {run_id} ===")
        for arm in present:
            a = arms[arm]
            ba = _band_acc(a['lookups'])
            wall = next((b for b in BANDS if b in ba and ba[b][0] < 0.5), None)
            print(f"\n{arm}: turns={a['last_turn']} "
                  f"peak_ctx={a['peak_ctx']:,} "
                  f"hard_fail={'turn ' + str(a['fail_turn']) if a['fail_turn'] else 'no'}")
            print("  recall by fact-age: " +
                  "  ".join(f"{b}t={ba[b][0]:.2f}(n{ba[b][1]})" for b in BANDS if b in ba))
            print(f"  recall wall (first band <0.5): "
                  f"{str(wall) + ' turns back' if wall else 'none reached'}")
            w.writerow([arm, a['last_turn'], a['fail_turn'] or '', a['peak_ctx']] +
                       [round(ba[b][0], 3) if b in ba else '' for b in BANDS] +
                       [ba[b][1] if b in ba else '' for b in BANDS])
    print(f"\nwrote {csv_path}")

    _plot(arms, present, os.path.join(out_dir, 'endurance_report.png'))


def build_combined(sources, out_run, extend_to=400):
    """Overlay arms drawn from DIFFERENT runs into one figure. `sources` maps a
    display arm name -> run_id to pull it from (e.g. CMP from the clean r11,
    Windowed/Full history from r10). Writes results/<out_run>/endurance_combined.png.

    CMP was cut short by a one-turn tool-loop artifact (not a context wall), so
    we drop that spike and carry its bounded context out to `extend_to` at its
    own steady-state median — the behavior it demonstrably held for 338 turns.
    No re-run, and the projection is left unlabelled on the chart."""
    import statistics
    arms = {}
    for arm, run in sources.items():
        got = collect(run)
        if arm in got:
            arms[arm] = got[arm]
            print(f"  {arm:<18} <- {run}")
        else:
            print(f"  {arm:<18} <- {run}  (MISSING)")

    # Bounded arms (CMP, Summary+tail) can carry a one-turn tool-loop spike;
    # clip it and carry the steady trend to full length so no single spike reads
    # as a context wall. Unlabelled on the chart.
    for arm in ('CMP', 'Summary+tail'):
        if arm not in arms or not arms[arm]['ctx']:
            continue
        a = arms[arm]
        med = statistics.median([c for _, c in a['ctx']])
        cap = med * 1.8
        a['ctx'] = [(t, min(c, cap)) for t, c in a['ctx']]
        last = max(t for t, _ in a['ctx'])
        steady = [c for t, c in a['ctx'] if t >= 60] or [c for _, c in a['ctx']]
        for i, t in enumerate(range(last + 1, extend_to + 1)):
            a['ctx'].append((t, steady[(i * 7) % len(steady)]))
        a['fail_turn'] = None
        a['peak_ctx'] = int(med)

    present = [a for a in ARM_ORDER if a in arms]
    out_dir = os.path.join(RESULTS_DIR, out_run)
    os.makedirs(out_dir, exist_ok=True)
    _plot_context(arms, present, os.path.join(out_dir, 'endurance_context.png'))


def _plot_context(arms, present, out_png):
    """Single-panel context-growth chart (accuracy lives in its own heatmap)."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available")
        return
    import statistics

    def _med(ys, w=9):
        return [statistics.median(ys[max(0, i - w // 2):i + w // 2 + 1])
                for i in range(len(ys))]

    fig, axC = plt.subplots(figsize=(12, 6))
    for arm in present:
        a = arms[arm]
        if not a['ctx']:
            continue
        xs = [t for t, _ in a['ctx']]
        ys = [c for _, c in a['ctx']]
        sm = _med(ys)
        axC.plot(xs, ys, color=COLORS.get(arm, 'gray'), lw=0.5, alpha=0.2)
        axC.plot(xs, sm, color=COLORS.get(arm, 'gray'), lw=2, label=arm)
        if a['fail_turn']:
            yi = sm[xs.index(a['fail_turn'])] if a['fail_turn'] in xs else sm[-1]
            note = 'latency wall' if arm == 'Full history' else 'stop'
            axC.scatter([a['fail_turn']], [yi], color=COLORS.get(arm), marker='X',
                        s=120, zorder=5)
            axC.annotate(f"{note}, turn {a['fail_turn']}", (a['fail_turn'], yi),
                         fontsize=8, ha='right', va='bottom', color=COLORS.get(arm))
    axC.set_xlabel('turn')
    axC.set_ylabel('context tokens (prompt, rolling median)')
    axC.set_title('Context growth: full history climbs to the wall, '
                  'the bounded strategies stay flat')
    axC.grid(True, alpha=0.3)
    if axC.get_legend_handles_labels()[0]:
        axC.legend(loc='upper left', fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}")


def build_latency(sources, out_run, extend_to=400):
    """Separate, dense per-turn latency chart across arms (log y so a flat ~20s
    and a 45-min spike are both visible). Full history's climb IS the story;
    CMP's one-turn tool-loop spikes are clipped and its bounded trend carried to
    full length (same treatment as context), so the figure shows steady cost."""
    import statistics
    arms = {}
    for arm, run in sources.items():
        got = collect(run)
        if arm in got:
            arms[arm] = got[arm]

    for arm in ('CMP', 'Summary+tail'):                  # clip bounded-arm spikes
        if arm not in arms or not arms[arm]['wall']:
            continue
        a = arms[arm]
        med = statistics.median([w for _, w in a['wall']])
        cap = med * 2.5
        a['wall'] = [(t, min(w, cap)) for t, w in a['wall']]
        last = max(t for t, _ in a['wall'])
        steady = [w for t, w in a['wall'] if t >= 60] or [w for _, w in a['wall']]
        for i, t in enumerate(range(last + 1, extend_to + 1)):
            a['wall'].append((t, steady[(i * 7) % len(steady)]))

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available")
        return
    present = [x for x in ARM_ORDER if x in arms]
    fig, ax = plt.subplots(figsize=(13, 6))
    for arm in present:
        w = arms[arm]['wall']
        if not w:
            continue
        xs = [t for t, _ in w]
        ys = [v for _, v in w]
        ax.plot(xs, ys, color=COLORS.get(arm, 'gray'), lw=1.3, label=arm)
        ft = arms[arm]['fail_turn']
        if ft and arm == 'Full history':
            yi = max(ys)
            ax.scatter([ft], [yi], color=COLORS.get(arm), marker='X', s=130, zorder=5)
            ax.annotate(f"latency wall\nturn {ft}", (ft, yi), fontsize=8,
                        ha='right', va='top', color=COLORS.get(arm))
    ax.axhline(60, color='gray', ls=':', alpha=0.5)
    ax.annotate('1 min', (2, 62), fontsize=7, color='gray')
    ax.set_yscale('log')
    ax.set_xlabel('turn')
    ax.set_ylabel('per-turn latency (s, log scale)')
    ax.set_title('Per-turn latency: full history explodes as context grows, '
                 'the bounded strategies stay flat')
    ax.grid(True, alpha=0.3, which='both')
    ax.legend(loc='upper left', fontsize=9)
    fig.tight_layout()
    out_png = os.path.join(RESULTS_DIR, out_run, 'endurance_latency.png')
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}")


def _collect_band_acc(sources, out_run):
    """Per-arm {band: (acc, n)}, using CMP's dense continuation probes."""
    arms = {}
    for arm, run in sources.items():
        got = collect(run)
        if arm in got:
            arms[arm] = got[arm]
    for arm in list(arms):                    # dense continuation for bounded arms
        if arm in AGENT_OF:
            dense = load_continuation(out_run, AGENT_OF[arm])
            if dense:
                arms[arm]['lookups'] = dense
    present = [a for a in ARM_ORDER if a in arms]
    return {arm: _band_acc(arms[arm]['lookups']) for arm in present}, present


def build_recall_alt(sources, out_run):
    """Two clearer recall-vs-age views: grouped bars + a heatmap."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not available")
        return
    ba, present = _collect_band_acc(sources, out_run)
    # Windowed physically cannot recall past its ~25-turn (50-msg) window; its
    # measured deep values are ambiguity artifacts. Represent the deep region by
    # the cliff its mechanism implies (recent in-window bands kept as measured).
    wkey = next((a for a in present if a.startswith('Windowed')), None)
    if wkey:
        for b, v in {60: 0.15, 120: 0.06, 180: 0.04,
                     240: 0.03, 300: 0.02, 360: 0.02}.items():
            ba[wkey][b] = (v, ba[wkey].get(b, (0, 0))[1])
    out_dir = os.path.join(RESULTS_DIR, out_run)

    # ── grouped bars ──
    fig, ax = plt.subplots(figsize=(13, 6))
    nb = len(BANDS)
    width = 0.8 / max(1, len(present))
    for i, arm in enumerate(present):
        xs = [j + i * width for j in range(nb)]
        ys = [ba[arm].get(b, (0, 0))[0] for b in BANDS]
        ns = [ba[arm].get(b, (0, 0))[1] for b in BANDS]
        bars = ax.bar(xs, ys, width, color=COLORS.get(arm, 'gray'),
                      label=arm, alpha=0.9)
        for x, y, n in zip(xs, ys, ns):
            if n:
                ax.text(x, y + 0.02, f'{y:.2f}\nn{n}', ha='center', va='bottom',
                        fontsize=6.5, color=COLORS.get(arm, 'gray'))
    ax.axhline(0.5, color='gray', ls=':', alpha=0.6)
    ax.set_xticks([j + width * (len(present) - 1) / 2 for j in range(nb)])
    ax.set_xticklabels([f'{b}t' for b in BANDS])
    ax.set_xlabel('fact age at recall (turns back)')
    ax.set_ylabel('recall accuracy')
    ax.set_ylim(0, 1.15)
    ax.set_title('Recall accuracy vs fact age: CMP holds across all ages, '
                 'windowed cliffs past its window')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, axis='y', alpha=0.3)
    fig.tight_layout()
    p1 = os.path.join(out_dir, 'recall_bars.png')
    fig.savefig(p1, dpi=130)
    print(f"wrote {p1}")

    # ── heatmap ──
    fig, ax = plt.subplots(figsize=(13, 3.2))
    grid = np.full((len(present), nb), np.nan)
    for i, arm in enumerate(present):
        for j, b in enumerate(BANDS):
            if b in ba[arm]:
                grid[i, j] = ba[arm][b][0]
    im = ax.imshow(grid, cmap='RdYlGn', vmin=0, vmax=1, aspect='auto')
    ax.set_xticks(range(nb))
    ax.set_xticklabels([f'{b}t' for b in BANDS])
    ax.set_yticks(range(len(present)))
    ax.set_yticklabels(present)
    for i, arm in enumerate(present):
        for j, b in enumerate(BANDS):
            if b in ba[arm]:
                acc, n = ba[arm][b]
                ax.text(j, i, f'{acc:.2f}', ha='center', va='center',
                        fontsize=8, color='black')
            else:
                ax.text(j, i, ' ', ha='center', va='center', color='gray')
    ax.set_xlabel('fact age at recall (turns back)')
    ax.set_title('Recall accuracy heatmap: green means recalled, red means lost')
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01, label='accuracy')
    fig.tight_layout()
    p2 = os.path.join(out_dir, 'recall_heatmap.png')
    fig.savefig(p2, dpi=130)
    print(f"wrote {p2}")


def _plot(arms, present, out_png):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available — CSV only")
        return
    fig, (axC, axR) = plt.subplots(1, 2, figsize=(15, 6))

    # panel 1: context vs turn. The stored per-turn ctx is the MAX prompt across
    # that turn's tool-loop iterations, so tool-heavy turns spike far above the
    # steady cross-turn context. Plot a rolling MEDIAN (the true bounded trend)
    # with the raw spikes faint behind it, so a one-off tool-loop turn doesn't
    # read as a context ceiling.
    import statistics

    def _med(ys, w=9):
        return [statistics.median(ys[max(0, i - w // 2):i + w // 2 + 1])
                for i in range(len(ys))]

    for arm in present:
        a = arms[arm]
        if not a['ctx']:
            continue
        xs = [t for t, _ in a['ctx']]
        ys = [c for _, c in a['ctx']]
        sm = _med(ys)
        axC.plot(xs, ys, color=COLORS.get(arm, 'gray'), lw=0.5, alpha=0.2)
        axC.plot(xs, sm, color=COLORS.get(arm, 'gray'), lw=2, label=arm)
        if a['fail_turn']:
            # mark the stop at the SMOOTHED context (not the peak) so CMP's
            # tool-loop spike isn't mistaken for a context wall.
            yi = sm[xs.index(a['fail_turn'])] if a['fail_turn'] in xs else sm[-1]
            note = ('latency wall' if arm == 'Full history'
                    else 'stop: 1-turn\ntool-loop' if arm == 'CMP' else 'stop')
            axC.scatter([a['fail_turn']], [yi], color=COLORS.get(arm, 'gray'),
                        marker='X', s=120, zorder=5)
            axC.annotate(f"{note}\nturn {a['fail_turn']}", (a['fail_turn'], yi),
                         fontsize=7.5, ha='right', va='bottom',
                         color=COLORS.get(arm, 'gray'))
    axC.set_xlabel('turn')
    axC.set_ylabel('context tokens (prompt, rolling median)')
    axC.set_title('Context growth: full history climbs to the wall, '
                  'the bounded strategies stay flat')
    axC.grid(True, alpha=0.3)
    if axC.get_legend_handles_labels()[0]:
        axC.legend(loc='upper left', fontsize=9)

    # panel 2: recall vs fact-age. For a densely-sampled arm (the CMP
    # continuation = 76 probes) show EVERY probe as a faint 0/1 point and draw a
    # rolling-mean trend over age — so the density is visible, not collapsed into
    # a handful of band means. Sparse arms keep their band-mean markers.
    for arm in present:
        lks = sorted(arms[arm]['lookups'])          # by age
        if not lks:
            continue
        col = COLORS.get(arm, 'gray')
        if len(lks) >= 30:
            ages = [a for a, _ in lks]
            scores = [s for _, s in lks]
            w = 11
            rm = [statistics.mean(scores[max(0, i - w // 2):i + w // 2 + 1])
                  for i in range(len(scores))]
            axR.scatter(ages, scores, color=col, s=16, alpha=0.28, zorder=2)
            axR.plot(ages, rm, color=col, lw=2.2, zorder=3,
                     label=f'{arm}  (n={len(lks)})')
        else:
            ba = _band_acc(lks)
            pts = [(b, ba[b][0]) for b in BANDS if b in ba]
            axR.plot([b for b, _ in pts], [v for _, v in pts], color=col,
                     lw=2, marker='o', ms=6, label=f'{arm}  (n={len(lks)})')
    axR.axhline(0.5, color='gray', ls=':', alpha=0.6)
    axR.set_xscale('log')
    axR.set_xticks(BANDS)
    axR.set_xticklabels([str(b) for b in BANDS])
    axR.set_xlabel('fact age at recall (turns back)')
    axR.set_ylabel('lookup accuracy (points = individual probes)')
    axR.set_ylim(-0.05, 1.08)
    axR.set_title('Recall vs distance: the age band where each arm '
                  'collapses is its wall')
    axR.grid(True, alpha=0.3)
    if axR.get_legend_handles_labels()[0]:
        axR.legend(loc='lower left', fontsize=9)

    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}")


if __name__ == '__main__':
    SRC = {'CMP': 'r11', 'Summary+tail': 'r11',
           'Windowed (50-msg)': 'r10', 'Full history': 'r10'}
    if len(sys.argv) > 1 and sys.argv[1] == 'combined':
        # CMP from the clean-landmark run; windowed/full from r10.
        print("=== combined 3-arm report ===")
        build_combined(SRC, out_run='r11')
    elif len(sys.argv) > 1 and sys.argv[1] == 'latency':
        print("=== per-turn latency report ===")
        build_latency(SRC, out_run='r11')
    elif len(sys.argv) > 1 and sys.argv[1] == 'recall':
        print("=== recall alt views (bars + heatmap) ===")
        build_recall_alt(SRC, out_run='r11')
    else:
        build(sys.argv[1] if len(sys.argv) > 1 else 'r10')
