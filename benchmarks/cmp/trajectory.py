"""Per-turn trajectory for CMP paper validation.

For a run, emits — per turn index, averaged across seeds, for each arm (CMP,
FullHistory, and SumTail if present):
  - context tokens used that turn (the prompt the agent LLM saw)
  - probe accuracy at probe turns

Outputs results/<run_id>/trajectory.csv and trajectory.png (dual Y-axis:
context on the left, accuracy 0-1 on the right; X = turn). This is the figure
that shows CMP holding context bounded while accuracy stays high as the
session grows, vs the baseline's context climbing.

Usage: python trajectory.py <run_id> [<schedule>]
"""

import csv
import glob
import json
import os
import sys
from collections import defaultdict

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')


def _arm(run_id, agent):
    if agent == 'aisyah':
        return 'CMP'
    if agent == 'aisyah_sum':
        return 'SumTail'
    return 'SumTail' if run_id == 'r1' else 'FullHistory'


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def collect(run_id):
    """Return {arm: {turn: {'ctx':[], 'probe':[], 'wall':[], 'llm':[]}}} across
    seeds. 'wall' = end-to-end seconds per turn (as experienced, incl. poll
    overhead); 'llm' = pure model compute per turn (agent + CMP detector)."""
    per = defaultdict(lambda: defaultdict(
        lambda: {'ctx': [], 'probe': [], 'wall': [], 'llm': [],
                 'invalid': [], 'iters': []}))
    for f in glob.glob(os.path.join(RESULTS_DIR, run_id, 'metrics', 's*_aisyah*.json')):
        d = json.load(open(f))
        agent = d['summary']['agent']
        arm = _arm(run_id, agent)
        for r in d['turns']:
            t = r['turn']
            if r.get('main_prompt_tokens_peak'):
                per[arm][t]['ctx'].append(r['main_prompt_tokens_peak'])
            if r.get('probe_score') is not None:
                per[arm][t]['probe'].append(r['probe_score'])
            if r.get('wall_s') and r['wall_s'] > 0:
                per[arm][t]['wall'].append(r['wall_s'])
            llm_ms = (r.get('main_duration_ms') or 0) + (r.get('det_duration_ms') or 0)
            if llm_ms:
                per[arm][t]['llm'].append(llm_ms / 1000.0)
            per[arm][t]['invalid'].append(r.get('invalid_action_rate', 0.0))
            per[arm][t]['iters'].append(r.get('main_calls', 0))
    return per


def build(run_id):
    per = collect(run_id)
    if not per:
        raise SystemExit(f"no metrics for {run_id} (run metrics.py first)")
    arms = [a for a in ('CMP', 'FullHistory', 'SumTail') if a in per]
    turns = sorted({t for a in arms for t in per[a]})

    # ── CSV (chart-ready: one row per turn, columns per arm) ──
    out_dir = os.path.join(RESULTS_DIR, run_id)
    csv_path = os.path.join(out_dir, 'trajectory.csv')
    cols = ['turn']
    for a in arms:
        cols += [f'{a}_context', f'{a}_accuracy', f'{a}_cum_accuracy',
                 f'{a}_wall_s', f'{a}_llm_s', f'{a}_invalid_rate', f'{a}_iters']
    rows = []
    cum = {a: [] for a in arms}
    for t in turns:
        row = {'turn': t + 1}
        for a in arms:
            cell = per[a].get(t)
            if not cell:
                continue
            row[f'{a}_context'] = round(_mean(cell['ctx'])) if cell['ctx'] else ''
            acc = _mean(cell['probe']) if cell['probe'] else None
            row[f'{a}_accuracy'] = round(acc, 3) if acc is not None else ''
            if acc is not None:
                cum[a].append(acc)
            row[f'{a}_cum_accuracy'] = round(_mean(cum[a]), 3) if cum[a] else ''
            row[f'{a}_wall_s'] = round(_mean(cell['wall']), 1) if cell['wall'] else ''
            row[f'{a}_llm_s'] = round(_mean(cell['llm']), 1) if cell['llm'] else ''
            row[f'{a}_invalid_rate'] = round(_mean(cell['invalid']), 3) if cell['invalid'] else ''
            row[f'{a}_iters'] = round(_mean(cell['iters']), 1) if cell['iters'] else ''
        rows.append(row)
    with open(csv_path, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {csv_path}")

    _plot(run_id, arms, turns, per, out_dir)


def _linfit(xs, ys):
    """Ordinary least-squares slope/intercept (pure python, no numpy)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(v * v for v in xs)
    sxy = sum(a * b for a, b in zip(xs, ys))
    d = n * sxx - sx * sx
    if d == 0:
        return 0.0, sy / n
    m = (n * sxy - sx * sy) / d
    return m, (sy - m * sx) / n


def _smooth(ys, w=5):
    """Centered moving average over non-None values (keeps gaps as None)."""
    out = []
    half = w // 2
    for i in range(len(ys)):
        vals = [v for v in ys[max(0, i - half):i + half + 1] if v is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def _plot(run_id, arms, turns, per, out_dir):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available — CSV only")
        return
    colors = {'CMP': 'tab:blue', 'FullHistory': 'tab:red', 'SumTail': 'tab:gray'}
    label = {'CMP': 'CMP', 'FullHistory': 'Without CMP',
             'SumTail': 'Without CMP (summarize)'}
    x = [t + 1 for t in turns]
    xmax = max(x)
    xproj = 2 * xmax  # project the trend out to double the turns

    def series(a, key):
        return [(_mean(per[a][t][key]) if t in per[a] and per[a][t][key] else None)
                for t in turns]

    # 4 stacked panels: context / accuracy / latency / degradation
    fig, (axC, axA, axL, axD) = plt.subplots(
        4, 1, figsize=(12, 15), sharex=True,
        gridspec_kw={'height_ratios': [2, 1.6, 1, 1]})

    # ── panel 1: context on STEADY-STATE (non-probe) turns + projection ──
    # Probe turns trigger end-of-session retrieval spikes (recall/read_transcript
    # loading) that don't reflect normal per-turn context, so both the LINE and
    # the projection use working turns only; smoothing is applied after the
    # probe turns are removed so their spikes don't bleed into neighbours.
    for a in arms:
        raw = [(t + 1, _mean(per[a][t]['ctx'])) for t in turns
               if t in per[a] and per[a][t]['ctx'] and not per[a][t]['probe']]
        if len(raw) < 2:
            continue
        sx = [p[0] for p in raw]
        sy = _smooth([p[1] for p in raw])
        axC.plot(sx, sy, color=colors[a], lw=2, label=label[a])
        m, b = _linfit(sx, sy)
        pxs = list(range(sx[-1], xproj + 1))
        axC.plot(pxs, [max(0, m * xx + b) for xx in pxs],
                 color=colors[a], lw=1.5, ls=':', alpha=0.75)
    axC.axvline(xmax, color='gray', ls='--', alpha=0.4)
    axC.set_ylabel('context tokens (steady-state turns)')
    axC.grid(True, alpha=0.3)
    axC.legend(loc='upper left', fontsize=9)

    # ── panel 2: accuracy — CUMULATIVE (running-mean) so it reads as a clean
    #    trend instead of a per-probe zigzag; faint markers show each probe. ──
    for a in arms:
        pts = [(t + 1, _mean(per[a][t]['probe'])) for t in turns
               if t in per[a] and per[a][t]['probe']]
        if not pts:
            continue
        px = [p[0] for p in pts]
        run, cum = [], []
        for _, v in pts:
            run.append(v)
            cum.append(sum(run) / len(run))
        axA.scatter(px, [p[1] for p in pts], color=colors[a], s=22, alpha=0.35, zorder=2)
        axA.plot(px, cum, color=colors[a], lw=2.4, marker='o', ms=4, zorder=3, label=label[a])
        m, b = _linfit(px, cum)
        txs = list(range(px[-1], xproj + 1))
        axA.plot(txs, [max(0.0, min(1.0, m * xx + b)) for xx in txs],
                 color=colors[a], lw=1.4, ls=':', alpha=0.75, zorder=3)
    axA.axvline(xmax, color='gray', ls='--', alpha=0.4)
    axA.set_ylabel('cumulative accuracy')
    axA.set_ylim(-0.05, 1.08)
    axA.grid(True, alpha=0.3)
    axA.legend(loc='lower left', fontsize=9)

    # ── panel 3: LLM compute latency per turn ──
    for a in arms:
        axL.plot(x, series(a, 'llm'), color=colors[a], lw=1.5, marker='.', label=label[a])
    axL.set_ylabel('LLM compute (s/turn)')
    axL.grid(True, alpha=0.3)
    axL.legend(loc='upper left', fontsize=8)

    # ── panel 4: degradation — invalid-action rate (solid) + thrashing (dashed) ──
    axD2 = axD.twinx()
    for a in arms:
        axD.plot(x, series(a, 'invalid'), color=colors[a], lw=2,
                 label=f'{label[a]} invalid-action')
        axD2.plot(x, series(a, 'iters'), color=colors[a], lw=1, ls='--', alpha=0.55)
    axD.set_xlabel(f'turn  (dashed line = end of measured data at {xmax}; '
                   f'dotted = linear projection to {xproj})')
    axD.set_ylabel('invalid-action rate')
    axD2.set_ylabel('tool iters/turn')
    axD.set_ylim(-0.03, 1.03)
    axD.grid(True, alpha=0.3)
    axD.legend(loc='upper left', fontsize=7)

    fig.tight_layout()
    png = os.path.join(out_dir, 'trajectory.png')
    fig.savefig(png, dpi=130)
    print(f"wrote {png}")


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else 'r5')
