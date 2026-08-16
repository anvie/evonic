"""Deterministic probe scoring for the CMP benchmark.

A probe is a set of fact groups; each group lists accepted variants
(any_of). A group is matched when any variant appears in the normalized
response. Probe score = matched groups / total groups.
"""

import re


def normalize(text):
    """Lowercase and strip formatting noise so '$4,725' matches '4725' and
    '/reports/x/week-28.docx' survives markdown emphasis."""
    t = text.lower()
    t = t.replace('$', '').replace(',', '')
    t = re.sub(r'[*_`"“”‘’]', '', t)   # md emphasis + smart quotes
    # canonicalize clock times so "7 pm" == "7:00 pm" == "7:00pm":
    # drop a ":00" minutes part and the space before am/pm.
    t = re.sub(r'(\d{1,2}):00(\s*)([ap]m)', r'\1\3', t)
    t = re.sub(r'(\d{1,2})\s+([ap]m)', r'\1\2', t)
    t = re.sub(r'\s+', ' ', t)
    return t


def _variant_hit(variant, norm_response):
    v = normalize(variant)
    if not v:
        return False
    return v in norm_response


def score_probe(response, groups):
    """Return {'score': float, 'matched': int, 'total': int, 'misses': [names]}"""
    norm = normalize(response or '')
    matched, misses = 0, []
    for g in groups:
        if any(_variant_hit(v, norm) for v in g['any_of']):
            matched += 1
        else:
            misses.append(g['name'])
    total = len(groups)
    return {'score': matched / total if total else 1.0,
            'matched': matched, 'total': total, 'misses': misses}


# ── self-test ────────────────────────────────────────────────────────────────

def _selftest():
    cases = [
        # exact numbers with/without commas and $
        ("Revenue was **$4,725** as noted.", [{'name': 'rev', 'any_of': ['4725', '4,725']}], 1.0),
        ("we made about five thousand", [{'name': 'rev', 'any_of': ['4725', '4,725']}], 0.0),
        # file path in markdown code span
        ("Saved to `/reports/velora/week-28-s21.docx`.",
         [{'name': 'path', 'any_of': ['/reports/velora/week-28-s21.docx']}], 1.0),
        # case-insensitive name match, first-name-only variant
        ("Waiting on numbers from DANA before the financial section.",
         [{'name': 'person', 'any_of': ['Dana']},
          {'name': 'section', 'any_of': ['financial']}], 1.0),
        # partial: 1 of 2 groups
        ("The client is Mirabel Retail.",
         [{'name': 'client', 'any_of': ['Mirabel Retail']},
          {'name': 'amount', 'any_of': ['1200', '1,200']}], 0.5),
        # smart quotes around a title
        ("The working title is “Field Notes S21: After the Feed”.",
         [{'name': 'title', 'any_of': ['Field Notes S21: After the Feed']}], 1.0),
        # empty response
        ("", [{'name': 'x', 'any_of': ['anything']}], 0.0),
    ]
    for i, (resp, groups, expected) in enumerate(cases):
        got = score_probe(resp, groups)['score']
        assert abs(got - expected) < 1e-9, f"case {i}: expected {expected}, got {got} ({resp!r})"
    print(f"scoring selftest OK ({len(cases)} cases)")


if __name__ == '__main__':
    _selftest()
