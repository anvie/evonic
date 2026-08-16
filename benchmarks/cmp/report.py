"""Aggregate benchmark metrics into REPORT.md.

Arms:
  CMP          — agent `aisyah` (enable_cmp=1), from runs r1/r2
  FullHistory  — agent `aisyah_base` after parity fix (summarizer OFF), runs r3/r2
  SumTail      — agent `aisyah_base` in run r1 (unintended rolling-summarization
                 config; kept as an appendix baseline)

Usage: python report.py <out_label> <run_id> [<run_id> ...]
Writes results/<out_label>/REPORT.md (+ plots).
"""

import csv
import json
import os
import sys
from collections import defaultdict

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

INT_TURN_FIELDS = ('turn', 'main_calls', 'main_prompt_tokens_peak', 'main_prompt_tokens_sum',
                   'main_completion_tokens', 'main_duration_ms', 'det_calls',
                   'det_prompt_tokens', 'det_completion_tokens', 'det_duration_ms',
                   'aux_calls', 'aux_tokens')


def _arm(run_id, agent):
    if agent == 'aisyah':
        return 'CMP'
    if agent == 'aisyah_sum':
        return 'SumTail'
    # aisyah_base: full history — except run r1, where it accidentally ran
    # with the rolling summarizer enabled
    return 'SumTail' if run_id == 'r1' else 'FullHistory'


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pctile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def _fmt(x, nd=2):
    if x is None:
        return '—'
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return f"{x:,}" if isinstance(x, int) and abs(x) >= 1000 else str(x)


def _pct(x):
    return '—' if x is None else f"{100 * x:.1f}%"


def load(run_ids):
    summaries, turns = [], []
    for rid in run_ids:
        mdir = os.path.join(RESULTS_DIR, rid, 'metrics')
        with open(os.path.join(mdir, 'summary.json')) as f:
            for s in json.load(f):
                s['run_id'] = rid
                s['arm'] = _arm(rid, s['agent'])
                summaries.append(s)
        with open(os.path.join(mdir, 'turns.csv')) as f:
            for row in csv.DictReader(f):
                for k in INT_TURN_FIELDS:
                    row[k] = int(row[k] or 0)
                row['wall_s'] = float(row['wall_s'] or 0)
                row['probe_score'] = float(row['probe_score']) if row['probe_score'] else None
                row['probe_topics_before'] = (int(row['probe_topics_before'])
                                              if row.get('probe_topics_before') else None)
                row['run_id'] = rid
                row['arm'] = _arm(rid, row['agent'])
                turns.append(row)
    return summaries, turns


def _sum_stat(ss, f):
    return _mean([f(s) for s in ss])


def _headline_rows(summaries, turns, arms, schedules=None):
    """Rows of (label, {arm: value_str})."""
    out = []

    def flt(arm):
        r = [s for s in summaries if s['arm'] == arm]
        if schedules:
            r = [s for s in r if s['schedule'] in schedules]
        return r

    def tflt(arm):
        r = [t for t in turns if t['arm'] == arm]
        if schedules:
            r = [t for t in r if t['schedule'] in schedules]
        return r

    def row(label, f, fmt=_fmt):
        out.append((label, {a: fmt(_sum_stat(flt(a), f)) for a in arms}))

    def trow(label, f):
        out.append((label, {a: f(tflt(a)) for a in arms}))

    row('Probe accuracy (all probes)', lambda s: s['probe_accuracy'], _pct)
    row('Return fidelity', lambda s: s['return_fidelity'], _pct)
    row('Dependency fidelity', lambda s: s['dependency_fidelity'], _pct)
    row('Session-end fidelity', lambda s: s['session_end_fidelity'], _pct)
    row('Task completion', lambda s: s['task_completion'], _pct)
    trow('Median context/turn (tok)',
         lambda ts: _fmt(_pctile([t['main_prompt_tokens_peak'] for t in ts], 50), 0))
    trow('p90 context/turn (tok)',
         lambda ts: _fmt(_pctile([t['main_prompt_tokens_peak'] for t in ts], 90), 0))
    row('Peak context (tok)', lambda s: s['peak_prompt_tokens'],
        lambda x: _fmt(x, 0))
    row('Prompt tokens/session', lambda s: s['total_prompt_tokens'], lambda x: _fmt(x, 0))
    row('Aux tokens/session (subagents+summarizer+memory)',
        lambda s: s.get('aux_tokens_total', 0), lambda x: _fmt(x, 0))
    row('Est. cost/session (USD, proxy)', lambda s: s['cost_usd'], lambda x: _fmt(x, 4))
    trow('Mean LLM calls/turn', lambda ts: _fmt(_mean([t['main_calls'] for t in ts]), 2))
    trow('Runaway turns (>20 calls)', lambda ts: str(sum(1 for t in ts if t['main_calls'] > 20)))
    row('Mean turn latency (s)', lambda s: s['mean_wall_s'], lambda x: _fmt(x, 1))
    return out


def _table(w, rows, arms):
    w("| Metric | " + " | ".join(arms) + " |")
    w("|---" * (len(arms) + 1) + "|")
    for label, vals in rows:
        w(f"| {label} | " + " | ".join(vals.get(a, '—') for a in arms) + " |")
    w("")


def build_report(out_label, run_ids):
    summaries, turns = load(run_ids)
    core = [s['schedule'] for s in summaries if not s['schedule'].startswith('s6')]
    core_scheds = sorted(set(core))
    out = []
    w = out.append

    w(f"# CMP Benchmark Report — runs {', '.join(run_ids)}\n")
    w("**Arms.** *CMP*: agent `aisyah`, `enable_cmp=1`. *FullHistory*: identical "
      "clone `aisyah_base` with CMP off and NO summarization (linear full "
      "transcript) — the paper's full-context baseline. *SumTail* (appendix): the "
      "same clone as accidentally first configured, with rolling summarization + "
      "message tail — kept because it matches the paper's monolithic-summarization "
      "baseline. Backbone for all arms and for the CMP boundary detector: local "
      "Qwen3.5-9B (llama.cpp). Metrics come from recorded LLM traces (exact "
      "per-call usage), the token-monitor event log (detector/sub-agent/"
      "summarizer costs), CMP session state, and deterministic scripted probes.\n")

    # ── primary: CMP vs FullHistory on core schedules ──
    w("## Primary comparison — CMP vs full history (schedules s1–s5)\n")
    w("CMP rows come from run r1, FullHistory from run r3; identical scripted "
      "sessions (same schedules/seeds), facts salted per run.\n")
    _table(w, _headline_rows(summaries, turns, ['CMP', 'FullHistory'], core_scheds),
           ['CMP', 'FullHistory'])

    # ── per-schedule ──
    w("### Per-schedule breakdown\n")
    w("| Schedule | Arm | Probe acc | Return fid | Median ctx | Peak ctx | Prompt tok | Runaways |")
    w("|---|---|---|---|---|---|---|---|")
    for sch in core_scheds:
        for arm in ('CMP', 'FullHistory'):
            ss = [s for s in summaries if s['schedule'] == sch and s['arm'] == arm]
            ts = [t for t in turns if t['schedule'] == sch and t['arm'] == arm]
            if not ss:
                continue
            w(f"| {sch} | {arm} | {_pct(_sum_stat(ss, lambda s: s['probe_accuracy']))} "
              f"| {_pct(_sum_stat(ss, lambda s: s['return_fidelity']))} "
              f"| {_fmt(_pctile([t['main_prompt_tokens_peak'] for t in ts], 50), 0)} "
              f"| {_fmt(_sum_stat(ss, lambda s: s['peak_prompt_tokens']), 0)} "
              f"| {_fmt(_sum_stat(ss, lambda s: s['total_prompt_tokens']), 0)} "
              f"| {sum(1 for t in ts if t['main_calls'] > 20)} |")
    w("")

    # ── probe accuracy vs topics ──
    w("### Probe accuracy vs. number of topics\n")
    w("| Topics before probe | CMP | FullHistory | n |")
    w("|---|---|---|---|")
    by_topics = defaultdict(lambda: defaultdict(list))
    for t in turns:
        if t['probe_score'] is not None and t['probe_topics_before']:
            by_topics[t['probe_topics_before']][t['arm']].append(t['probe_score'])
    for k in sorted(by_topics):
        b = by_topics[k]
        w(f"| {k} | {_pct(_mean(b['CMP']))} | {_pct(_mean(b['FullHistory']))} "
          f"| {len(b['CMP']) + len(b['FullHistory'])} |")
    w("")

    # ── context growth ──
    w("### Context growth (median prompt tokens by turn index, s5_marathon)\n")
    w("| Turn | CMP | FullHistory |")
    w("|---|---|---|")
    by_turn = defaultdict(lambda: defaultdict(list))
    for t in turns:
        if t['schedule'] == 's5_marathon' and t['main_prompt_tokens_peak']:
            by_turn[t['turn']][t['arm']].append(t['main_prompt_tokens_peak'])
    for idx in sorted(by_turn):
        b = by_turn[idx]
        w(f"| {idx + 1} | {_fmt(_pctile(b['CMP'], 50), 0)} "
          f"| {_fmt(_pctile(b['FullHistory'], 50), 0)} |")
    w("")

    # ── long-haul (s6) ──
    s6 = [s for s in summaries if s['schedule'].startswith('s6')]
    if s6:
        w("## Long-haul regime — s6 (38 turns, 10 topics, tools allowed)\n")
        _table(w, _headline_rows(summaries, turns, ['CMP', 'FullHistory'], ['s6_longhaul']),
               ['CMP', 'FullHistory'])
        w("### Context growth by turn (s6)\n")
        w("| Turn | CMP | FullHistory |")
        w("|---|---|---|")
        by_turn6 = defaultdict(lambda: defaultdict(list))
        for t in turns:
            if t['schedule'] == 's6_longhaul' and t['main_prompt_tokens_peak']:
                by_turn6[t['turn']][t['arm']].append(t['main_prompt_tokens_peak'])
        for idx in sorted(by_turn6):
            b = by_turn6[idx]
            w(f"| {idx + 1} | {_fmt(_pctile(b['CMP'], 50), 0)} "
              f"| {_fmt(_pctile(b['FullHistory'], 50), 0)} |")
        w("")

    # ── CMP internals ──
    cmp_s = [s for s in summaries if s['arm'] == 'CMP']
    w("## CMP internals\n")
    w("| Session | Paths | Switches | Branches | Det calls | Det tok | Det s | Aux tok |")
    w("|---|---|---|---|---|---|---|---|")
    for s in cmp_s:
        w(f"| {s['run_id']}:{s['run_key']} | {_fmt(s['cmp_paths'])} | {_fmt(s['cmp_switches'])} "
          f"| {_fmt(s['cmp_branches'])} | {_fmt(s['det_calls_total'])} "
          f"| {_fmt(s['det_tokens_total'])} | {_fmt(s['det_duration_ms_total'] / 1000, 1)} "
          f"| {_fmt(s.get('aux_tokens_total', 0))} |")
    w("")

    # ── boundary detection ──
    w("## Boundary detection vs scripted ground truth (CMP sessions)\n")
    conf = defaultdict(int)
    for t in turns:
        if t['arm'] == 'CMP' and t['det_route']:
            conf[(t['gt_route'], t['det_route'])] += 1
    routes = ['continue', 'return', 'dep_branch', 'indep_branch']
    w("| GT \\ Detector | " + " | ".join(routes) + " |")
    w("|---" * (len(routes) + 1) + "|")
    for gt in routes:
        w(f"| {gt} | " + " | ".join(str(conf.get((gt, d), 0)) for d in routes) + " |")
    n_det = sum(conf.values())
    agree = sum(v for (g, d), v in conf.items() if g == d)
    w(f"\nRoute agreement {agree}/{n_det} ({_pct(agree / n_det if n_det else None)}). "
      "Detector decisions recovered from harness logs; agent-initiated navigation "
      "and safety guards can override, so disagreement ≠ final misrouting.\n")
    # per-class recall
    w("Per-class recall: " + ", ".join(
        f"{gt} {_pct((conf.get((gt, gt), 0) / n) if (n := sum(v for (g, _), v in conf.items() if g == gt)) else None)}"
        for gt in routes) + ".\n")

    # ── appendix: SumTail ──
    st = [s for s in summaries if s['arm'] == 'SumTail']
    if st:
        w("## Appendix — rolling summarization + tail baseline (run r1)\n")
        w("The first baseline run unintentionally had the platform's rolling "
          "summarizer enabled (threshold 3, tail 20): every ~3 turns the history "
          "is compacted into a bullet summary plus a short message tail. This "
          "matches the paper's *monolithic summarization* baseline, so it is "
          "reported here as a third arm.\n")
        _table(w, _headline_rows(summaries, turns, ['CMP', 'SumTail', 'FullHistory'],
                                 core_scheds),
               ['CMP', 'SumTail', 'FullHistory'])

    path = os.path.join(RESULTS_DIR, out_label)
    os.makedirs(path, exist_ok=True)
    report_path = os.path.join(path, 'REPORT.md')
    with open(report_path, 'w') as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {report_path}")
    _plots(out_label, turns)
    return report_path


def _plots(out_label, turns):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available — skipping plots")
        return
    scheds = sorted({t['schedule'] for t in turns})
    fig, axes = plt.subplots(1, len(scheds), figsize=(4.5 * len(scheds), 4), squeeze=False)
    colors = {'CMP': 'tab:blue', 'FullHistory': 'tab:red', 'SumTail': 'tab:gray'}
    for ax, sch in zip(axes[0], scheds):
        pts = defaultdict(lambda: defaultdict(list))
        for t in turns:
            if t['schedule'] == sch and t['main_prompt_tokens_peak']:
                pts[t['arm']][t['turn']].append(t['main_prompt_tokens_peak'])
        for arm, series in pts.items():
            xs = sorted(series)
            ys = [_pctile(series[x], 50) for x in xs]
            ax.plot([x + 1 for x in xs], ys, marker='o', ms=3, label=arm,
                    color=colors.get(arm))
        ax.set_title(sch, fontsize=9)
        ax.set_xlabel('turn')
        ax.set_ylabel('median prompt tokens')
        ax.legend(fontsize=7)
    fig.tight_layout()
    out = os.path.join(RESULTS_DIR, out_label, 'context_growth.png')
    fig.savefig(out, dpi=120)
    print(f"wrote {out}")


if __name__ == '__main__':
    label = sys.argv[1] if len(sys.argv) > 1 else 'final'
    runs = sys.argv[2:] or ['r1', 'r3']
    build_report(label, runs)
