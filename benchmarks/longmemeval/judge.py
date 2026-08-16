"""LLM-judge LongMemEval hypotheses -> per-type per-arm accuracy.

Adapts the official evaluate_qa.py judge prompts (correct/incorrect binary,
type-aware) to run on deepseek-v4-flash instead of GPT-4o. Scores every
hypotheses_<agent>.jsonl in results/<run-id>/ and writes judged_<agent>.jsonl
plus a summary table.

Usage: python judge.py --run-id lme1 --variant s
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ingest  # noqa: E402  (preloads SECRET_KEY + repo sys.path)
from models.db import db                      # noqa: E402
from backend.llm_client import LLMClient      # noqa: E402

AGENT_LABEL = {'aisyah': 'CMP', 'aisyah_base': 'Windowed',
               'aisyah_sum': 'Summary+tail', 'aisyah_full': 'Full history'}


def _judge_client():
    return LLMClient(db.get_model_by_id('custom/deepseek-v4-flash'))


def judge_prompt(qtype, question, answer, hypothesis, q_date=''):
    """Type-aware binary judge prompt (mirrors LongMemEval's evaluate_qa)."""
    if qtype == 'temporal-reasoning':
        extra = (" In addition, do not penalize off-by-one errors for the number "
                 "of days. If the question asks for the number of days/weeks/"
                 "months, etc., and the model makes off-by-one errors (e.g., "
                 "predicting 19 days when the answer is 18), the model's "
                 "response is still correct.")
    elif qtype == 'knowledge-update':
        extra = (" The model's response should reflect the LATEST information; "
                 "an answer that only mentions the outdated value is incorrect.")
    else:
        extra = ""
    return (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response is equivalent to the correct "
        "answer or contains all the intermediate steps to get the correct "
        "answer, you should also answer yes. If the response only contains a "
        "subset of the information required by the answer, answer no." + extra +
        f"\n\nQuestion: {question}\n\nCorrect Answer: {answer}\n\n"
        f"Model Response: {hypothesis}\n\nIs the model response correct? "
        "Answer yes or no only.")


def judge_one(client, q, hypothesis):
    if '_abs' in q['question_id']:
        # abstention: correct iff the model declines / says it doesn't know
        prompt = (
            "I will give you an unanswerable question and a response from a "
            "model. Please answer yes if the model correctly identifies the "
            "question as unanswerable (e.g. says it does not know or has no "
            "such information). Otherwise answer no.\n\n"
            f"Question: {q['question']}\n\nModel Response: {hypothesis}\n\n"
            "Does the model correctly identify the question as unanswerable? "
            "Answer yes or no only.")
    else:
        prompt = judge_prompt(q['question_type'], q['question'], q['answer'],
                              hypothesis)
    resp = client.chat_completion([{'role': 'user', 'content': prompt}],
                                  enable_thinking=False, max_tokens=200)
    content = ((resp.get('response', {}).get('choices') or [{}])[0]
               .get('message', {}).get('content') or '').strip().lower()
    return content.startswith('yes')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--variant', default='s', choices=['s', 'oracle'])
    args = ap.parse_args()

    fname = 'longmemeval_s.json' if args.variant == 's' else 'longmemeval_oracle.json'
    data = {q['question_id']: q for q in
            json.load(open(os.path.join(HERE, 'data', fname)))}
    out_dir = os.path.join(HERE, 'results', args.run_id)
    client = _judge_client()

    summary = {}
    import glob
    for hp in sorted(glob.glob(os.path.join(out_dir, 'hypotheses_*.jsonl'))):
        agent = os.path.basename(hp)[len('hypotheses_'):-len('.jsonl')]
        arm = AGENT_LABEL.get(agent, agent)
        judged_path = os.path.join(out_dir, f'judged_{agent}.jsonl')
        done = {}
        if os.path.exists(judged_path):
            done = {json.loads(l)['question_id']: json.loads(l)
                    for l in open(judged_path) if l.strip()}
        jf = open(judged_path, 'a')
        rows = [json.loads(l) for l in open(hp) if l.strip()]
        # last occurrence wins (reruns append)
        rows = list({r['question_id']: r for r in rows}.values())
        for r in rows:
            if r['question_id'] in done:
                continue
            q = data.get(r['question_id'])
            if q is None:
                continue
            hyp = r.get('hypothesis') or ''
            label = judge_one(client, q, hyp) if hyp.strip() else False
            rec = {'question_id': r['question_id'],
                   'question_type': q['question_type'],
                   'correct': bool(label), 'wall_s': r.get('wall_s'),
                   'failed': not hyp.strip()}
            jf.write(json.dumps(rec) + '\n')
            jf.flush()
            done[r['question_id']] = rec
            print(f"  {arm:<13} {q['question_type'][:16]:<16} "
                  f"{'OK ' if label else 'MISS'} {r['question_id']}")
        jf.close()
        # rollup
        from collections import defaultdict
        by = defaultdict(list)
        for rec in done.values():
            by[rec['question_type']].append(rec['correct'])
            by['__overall__'].append(rec['correct'])
        summary[arm] = {t: (sum(v) / len(v), len(v)) for t, v in by.items()}

    print(f"\n=== {args.run_id} accuracy by arm x type ===")
    types = sorted({t for s in summary.values() for t in s if t != '__overall__'})
    hdr = 'arm'.ljust(14) + ''.join(t[:14].ljust(16) for t in types) + 'OVERALL'
    print(hdr)
    for arm, s in summary.items():
        row = arm.ljust(14)
        for t in types:
            if t in s:
                acc, n = s[t]
                row += f"{acc:.2f} (n{n})".ljust(16)
            else:
                row += '-'.ljust(16)
        if '__overall__' in s:
            acc, n = s['__overall__']
            row += f"{acc:.2f} (n{n})"
        print(row)
    json.dump({a: {t: v for t, v in s.items()} for a, s in summary.items()},
              open(os.path.join(out_dir, 'summary.json'), 'w'), indent=1)
    print(f"\nwrote {os.path.join(out_dir, 'summary.json')}")


if __name__ == '__main__':
    main()
