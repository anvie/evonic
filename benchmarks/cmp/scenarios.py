"""Deterministic scenario generator for the CMP benchmark.

Each scenario is a scripted multi-task session (paper §5): task instances are
woven into one session under an interleaving schedule. Facts are injected via
user messages (all fictional, seeded per run so re-runs can't leak answers
through agent memory), and probes test them later with known ground truth.

Every turn carries a ground-truth boundary label (continue / return /
dep_branch / indep_branch + target task) so detector accuracy can be scored
against the script.
"""

import random

SCHEDULES = ('s1_sequential', 's2_single_return', 's3_dep_branch', 's4_oscillation',
             's5_marathon')

# schedules that skip the chat-only preamble (realistic tool-using regime)
NO_GROUND_RULES = {'s6_longhaul', 's7_crosstool', 's8_megamarathon'}

# ── fictional fact pools (dummy values only — never real people/companies) ──
COMPANIES = ['Velora Dynamics', 'Kestrel Media', 'Bluefir Labs',
             'Quartzline Foods', 'Novapine Systems', 'Harkline Logistics']
PEOPLE = ['Dana Wheeler', 'Milo Trent', 'Sasha Bloom',
          'Theo Marsh', 'Ivy Callahan', 'Ruben Ortiz']
PRODUCTS = ['GlideMax 3', 'FernPad Mini', 'Corvid Deck', 'LumenJar', 'TerraSpool']
CLIENTS = ['Ostrell & Co', 'Mirabel Retail', 'Tandem Grove', 'Pellworth Group']
BLOG_TOPICS = ['on-device AI assistants', 'slow productivity', 'urban beekeeping',
               'self-hosted photo backups', 'learning piano as an adult']
BLOG_SITES = ['fernweh.blog', 'quietstack.net', 'copperpencil.io']
CITIES = ['Lisbon', 'Osaka', 'Tallinn', 'Cusco', 'Ljubljana']
HOTELS = ['The Copper Fern', 'Marlin House', 'Villa Sequoia', 'Hotel Bramble']
PENDING_SECTIONS = ['financial', 'logistics', 'staffing', 'marketing']


GROUND_RULES = (
    "Session ground rules (apply to everything we do today): work in chat only. "
    "Draft all text directly in your replies — do NOT create, edit or save any "
    "files or artifacts, do NOT add tasks, and do NOT save notes to memory. "
    "When I mention a file path, just keep it as a reference for later.\n\n"
)


def _turn(task, route, message, probe=None, target=None):
    """route: continue|return|dep_branch|indep_branch; target: task label the
    route points at (for return/dep_branch)."""
    return {'task': task, 'gt_route': route, 'gt_target': target,
            'message': message, 'probe': probe}


def _num_variants(n):
    """Accepted spellings of an integer amount: 4725 / 4,725."""
    s = str(n)
    with_commas = f"{n:,}"
    return list({s, with_commas})


# ── task fact generation ─────────────────────────────────────────────────────

def _report_facts(rng, run_tag):
    week = rng.randint(20, 45)
    company = rng.choice(COMPANIES)
    slug = company.split()[0].lower()
    return {
        'company': company,
        'revenue': rng.randint(1000, 9000) * rng.choice([1, 10]) + rng.randint(100, 999),
        'product': rng.choice(PRODUCTS),
        'path': f"/reports/{slug}/week-{week}-{run_tag}.docx",
        'pending': rng.choice(PENDING_SECTIONS),
        'person': rng.choice(PEOPLE),
        'week': week,
    }


def _blog_facts(rng, run_tag):
    return {
        'site': rng.choice(BLOG_SITES),
        'topic': rng.choice(BLOG_TOPICS),
        'title': f"Field Notes {run_tag.upper()}: " + rng.choice(
            ['The Quiet Machine', 'Small Rooms, Big Ideas', 'After the Feed',
             'Ten Honest Drafts', 'The Unhurried Stack']),
        'audience': rng.choice(['indie developers', 'design students',
                                'remote team leads', 'hobbyist makers']),
    }


def _invoice_facts(rng, run_tag, k):
    return {
        'client': rng.choice(CLIENTS),
        'amount': rng.randint(800, 9500),
        'invnum': f"INV-{run_tag.upper()}-{rng.randint(100, 999)}-{k}",
        'due': f"August {rng.randint(2, 28)}, 2026",
    }


def _trip_facts(rng, run_tag):
    return {
        'city': rng.choice(CITIES),
        'hotel': rng.choice(HOTELS),
        'budget': rng.randint(1200, 4800),
        'days': rng.randint(4, 9),
    }


def _meeting_facts(rng, run_tag):
    attendees = rng.sample(PEOPLE, 3)
    return {
        'attendees': attendees,
        'owner': attendees[0],
        'decision': rng.choice(['switch the launch to a soft rollout',
                                'freeze new features until QA clears the backlog',
                                'move the weekly sync to Tuesdays',
                                'pilot the new vendor for one quarter']),
        'deadline': f"July {rng.randint(20, 31)}, 2026",
    }


# ── task turn builders ───────────────────────────────────────────────────────

def _report_turns(f, first_route='indep_branch', target=None):
    t = 'A'
    return [
        _turn(t, first_route,
              f"Let's work on the weekly report for {f['company']} (week {f['week']}). "
              f"Key numbers: revenue was ${f['revenue']:,} and the top product was "
              f"{f['product']}. We'll save the draft to {f['path']}. "
              "Start by proposing the report's section outline.", target=target),
        _turn(t, 'continue',
              "Good. Draft the intro paragraph, two or three sentences, and mention "
              "the exact revenue figure."),
        _turn(t, 'continue',
              f"Now write a short product-highlights section focused on {f['product']}."),
        _turn(t, 'continue',
              f"Important note: the {f['pending']} section can't be written yet — "
              f"we're waiting on numbers from {f['person']}. Mark it as pending "
              "in the outline and tell me what you recorded."),
    ]


def _report_finish_turns(f):
    return [
        _turn('A', 'continue',
              "Draft the closing summary paragraph for the report, referencing the "
              "revenue figure once more."),
        _turn('A', 'continue',
              f"Give me the final one-line status of the report: file location, "
              f"what's done, and what's still pending and why.",
              probe={'kind': 'task_completion', 'task': 'A', 'groups': [
                  {'name': 'path', 'any_of': [f['path']]},
                  {'name': 'pending_section', 'any_of': [f['pending']]},
                  {'name': 'person', 'any_of': [f['person'].split()[0]]},
              ]}),
    ]


def _blog_turns(f, first_route='indep_branch', target=None):
    t = 'B'
    return [
        _turn(t, first_route,
              f"Set the report aside for now — different thing. I want to write a blog "
              f"post for {f['site']} about {f['topic']}. Working title: \"{f['title']}\". "
              f"The audience is {f['audience']}. Sketch a five-point outline.",
              target=target),
        _turn(t, 'continue', "Write the opening hook paragraph."),
        _turn(t, 'continue',
              "Suggest three alternative titles. Keep the working title in the list too."),
        _turn(t, 'continue',
              "Pick the strongest title from that list and justify it in one line."),
        _turn(t, 'continue',
              f"Final check on the post plan: state the site we're publishing to, the "
              f"working title, and the audience, in one line.",
              probe={'kind': 'task_completion', 'task': 'B', 'groups': [
                  {'name': 'site', 'any_of': [f['site']]},
                  {'name': 'title', 'any_of': [f['title']]},
                  {'name': 'audience', 'any_of': [f['audience']]},
              ]}),
    ]


def _faq_turns(f, first_route='indep_branch', target=None):
    t = 'C'
    return [
        _turn(t, first_route,
              f"New task: a customer FAQ for our product {f['product']}, version "
              f"{f['version']}, priced at ${f['price']}. Draft the first three "
              "Q&A pairs (shipping, returns, warranty).", target=target),
        _turn(t, 'continue', "Add two more Q&A pairs about setup and compatibility."),
        _turn(t, 'continue',
              "Write the FAQ's one-paragraph intro. Mention the product name, "
              "version and price exactly.",
              probe={'kind': 'task_completion', 'task': 'C', 'groups': [
                  {'name': 'product', 'any_of': [f['product']]},
                  {'name': 'version', 'any_of': [f['version']]},
                  {'name': 'price', 'any_of': _num_variants(f['price'])},
              ]}),
    ]


def _faq_facts(rng, run_tag):
    return {
        'product': rng.choice(PRODUCTS),
        'version': f"{rng.randint(2, 7)}.{rng.randint(0, 9)}",
        'price': rng.randint(49, 899),
    }


def _return_to_report_probe(f, n_topics_before):
    return _turn('A', 'return',
                 "OK, let's get back to the weekly report now. Quick memory check "
                 "before we continue — from our earlier discussion only, no need for "
                 "me to repeat anything: (1) what was the exact revenue figure, "
                 "(2) which file are we saving the draft to, and (3) whose numbers "
                 "are we still waiting on, for which section?",
                 target='A',
                 probe={'kind': 'return_fidelity', 'task': 'A',
                        'topics_before': n_topics_before, 'groups': [
                     {'name': 'revenue', 'any_of': _num_variants(f['revenue'])},
                     {'name': 'path', 'any_of': [f['path']]},
                     {'name': 'person', 'any_of': [f['person'].split()[0]]},
                     {'name': 'pending_section', 'any_of': [f['pending']]},
                 ]})


def _session_end_probes(facts_by_task, order):
    """Final cross-task probes; `order` lists (task_label, kind) to probe."""
    turns = []
    fmap = {
        'A': lambda f: ({'groups': [
            {'name': 'company', 'any_of': [f['company']]},
            {'name': 'revenue', 'any_of': _num_variants(f['revenue'])},
            {'name': 'path', 'any_of': [f['path']]},
        ]}, f"the weekly report — which company was it for, what was the revenue "
            f"figure, and what's the draft file path?"),
        'B': lambda f: ({'groups': [
            {'name': 'title', 'any_of': [f['title']]},
            {'name': 'site', 'any_of': [f['site']]},
        ]}, "the blog post — what was the working title and which site is it for?"),
        'C': lambda f: ({'groups': [
            {'name': 'product', 'any_of': [f['product']]},
            {'name': 'price', 'any_of': _num_variants(f['price'])},
        ]}, "the customer FAQ — which product was it about and at what price?"),
    }
    for task in order:
        spec, question = fmap[task](facts_by_task[task])
        turns.append(_turn(task, 'return',
                           f"Before we wrap up, a recap question about {question} "
                           "Answer from this conversation only.",
                           target=task,
                           probe={'kind': 'session_end', 'task': task,
                                  'topics_before': len(facts_by_task),
                                  'groups': spec['groups']}))
    return turns


# ── schedules ────────────────────────────────────────────────────────────────

def s1_sequential(rng, run_tag):
    """A then B then C, no returns until session-end recap probes."""
    fa, fb, fc = _report_facts(rng, run_tag), _blog_facts(rng, run_tag), _faq_facts(rng, run_tag)
    turns = []
    turns += _report_turns(fa) + _report_finish_turns(fa)
    turns += _blog_turns(fb)
    turns += _faq_turns(fc)
    turns += _session_end_probes({'A': fa, 'B': fb, 'C': fc}, ['A', 'B'])
    return turns, {'A': fa, 'B': fb, 'C': fc}


def s2_single_return(rng, run_tag):
    """A partial -> B complete -> return to A with probes -> finish A."""
    fa, fb = _report_facts(rng, run_tag), _blog_facts(rng, run_tag)
    turns = []
    turns += _report_turns(fa)                       # A partial (4 turns)
    turns += _blog_turns(fb)                         # B complete (5 turns)
    turns.append(_return_to_report_probe(fa, n_topics_before=2))
    turns += _report_finish_turns(fa)                # finish A
    turns += _session_end_probes({'A': fa, 'B': fb}, ['B'])
    return turns, {'A': fa, 'B': fb}


def s3_dep_branch(rng, run_tag):
    """A report -> B1, B2 invoices billed from the same company (dep-branch)."""
    fa = _report_facts(rng, run_tag)
    i1, i2 = _invoice_facts(rng, run_tag, 1), _invoice_facts(rng, run_tag, 2)
    while i2['client'] == i1['client']:
        i2 = _invoice_facts(rng, run_tag, 2)
    turns = []
    turns += _report_turns(fa) + _report_finish_turns(fa)

    def invoice_turns(inv, label):
        return [
            _turn(label, 'dep_branch',
                  f"Now, for the report work we just wrapped up, I need to bill the "
                  f"client. Issue an invoice from that same company to {inv['client']}: "
                  f"amount ${inv['amount']:,}, invoice number {inv['invnum']}, due "
                  f"{inv['due']}. Confirm the details back to me first.",
                  target='A'),
            _turn(label, 'continue',
                  "Draft the full invoice text block. It must state the issuer company "
                  "name, the client, the amount, the invoice number and the due date.",
                  probe={'kind': 'dependency', 'task': label,
                         'topics_before': 2, 'groups': [
                      {'name': 'issuer_from_parent', 'any_of': [fa['company']]},
                      {'name': 'client', 'any_of': [inv['client']]},
                      {'name': 'amount', 'any_of': _num_variants(inv['amount'])},
                      {'name': 'invnum', 'any_of': [inv['invnum']]},
                  ]}),
            _turn(label, 'continue', "Add a one-line payment-terms note (net 30)."),
        ]

    turns += invoice_turns(i1, 'B1')
    turns += invoice_turns(i2, 'B2')
    turns.append(_return_to_report_probe(fa, n_topics_before=3))
    turns += _session_end_probes({'A': fa}, ['A'])
    return turns, {'A': fa, 'B1': i1, 'B2': i2}


def s4_oscillation(rng, run_tag):
    """Rapid A/B alternation (trip vs meeting), probe at every switch-back."""
    ft, fm = _trip_facts(rng, run_tag), _meeting_facts(rng, run_tag)
    A, B = 'A', 'B'
    turns = [
        _turn(A, 'indep_branch',
              f"Help me plan a {ft['days']}-day trip to {ft['city']}. Budget is "
              f"${ft['budget']:,} and I want to stay at {ft['hotel']}. Rough day-by-day "
              "shape first, just bullets."),
        _turn(A, 'continue', "Refine days 1 and 2 with morning/afternoon/evening slots."),
        _turn(B, 'indep_branch',
              f"Hold that thought — I need meeting notes written up. Attendees were "
              f"{', '.join(fm['attendees'])}. The decision: {fm['decision']}. "
              f"{fm['owner']} owns the follow-up, deadline {fm['deadline']}. "
              "Draft the summary."),
        _turn(B, 'continue', "Tighten it to three bullet points."),
        _turn(A, 'return', target=A, message=
              "Back to the trip. Quick check from earlier: what's the hotel and the "
              "total budget? Then plan day 3.",
              probe={'kind': 'return_fidelity', 'task': A, 'topics_before': 2,
                     'groups': [
                  {'name': 'hotel', 'any_of': [ft['hotel']]},
                  {'name': 'budget', 'any_of': _num_variants(ft['budget'])},
              ]}),
        _turn(A, 'continue', "Add a food shortlist for that day, three places or dish ideas."),
        _turn(B, 'return', target=B, message=
              "Switching again — on the meeting notes: remind me who owns the "
              "follow-up and the deadline, then append an 'open risks' line.",
              probe={'kind': 'return_fidelity', 'task': B, 'topics_before': 2,
                     'groups': [
                  {'name': 'owner', 'any_of': [fm['owner'].split()[0]]},
                  {'name': 'deadline', 'any_of': [fm['deadline']]},
              ]}),
        _turn(B, 'continue', "Rewrite the summary as an email to the team, short."),
        _turn(A, 'return', target=A, message=
              f"Trip again. Which city are we planning for, and how many days? "
              "Then sketch the final day including checkout.",
              probe={'kind': 'return_fidelity', 'task': A, 'topics_before': 2,
                     'groups': [
                  {'name': 'city', 'any_of': [ft['city']]},
                  {'name': 'days', 'any_of': [str(ft['days'])]},
              ]}),
        _turn(A, 'continue', "Compress the whole itinerary into a one-screen summary."),
        _turn(B, 'return', target=B, message=
              "Last switch: on the meeting — what was the decision we recorded? "
              "Add it as the subject line of that email.",
              probe={'kind': 'return_fidelity', 'task': B, 'topics_before': 2,
                     'groups': [
                  {'name': 'decision', 'any_of': [fm['decision']]},
              ]}),
        _turn(A, 'return', target=A, message=
              "And to close out the trip plan: state hotel, city, budget and day "
              "count in a single line I can paste into my notes.",
              probe={'kind': 'session_end', 'task': A, 'topics_before': 2,
                     'groups': [
                  {'name': 'hotel', 'any_of': [ft['hotel']]},
                  {'name': 'city', 'any_of': [ft['city']]},
                  {'name': 'budget', 'any_of': _num_variants(ft['budget'])},
                  {'name': 'days', 'any_of': [str(ft['days'])]},
              ]}),
    ]
    return turns, {A: ft, B: fm}


def s5_marathon(rng, run_tag):
    """Six topics with interleaved returns — stresses context growth and
    accuracy as the number of topics increases (probes tagged with
    topics_before 3..6)."""
    fa = _report_facts(rng, run_tag)
    fb = _blog_facts(rng, run_tag)
    fc = _faq_facts(rng, run_tag)
    ft = _trip_facts(rng, run_tag)
    fm = _meeting_facts(rng, run_tag)
    inv = _invoice_facts(rng, run_tag, 1)

    turns = []
    turns += _report_turns(fa)                                   # A (4)
    turns += _blog_turns(fb)[:3]                                 # B (3)
    turns += _faq_turns(fc)                                      # C (3)
    probe = _return_to_report_probe(fa, n_topics_before=3)       # back to A
    turns.append(probe)
    turns += _report_finish_turns(fa)                            # finish A (2)
    turns += [                                                   # D trip (3)
        _turn('D', 'indep_branch',
              f"Now something personal: help me plan a {ft['days']}-day trip to "
              f"{ft['city']}. Budget ${ft['budget']:,}, staying at {ft['hotel']}. "
              "Rough day-by-day bullets first."),
        _turn('D', 'continue', "Refine the first two days with time slots."),
        _turn('D', 'continue', "Add a packing list, ten items max."),
    ]
    turns += [                                                   # E meeting (2)
        _turn('E', 'indep_branch',
              f"Also, write up meeting notes: attendees were "
              f"{', '.join(fm['attendees'])}. Decision: {fm['decision']}. "
              f"{fm['owner']} owns follow-up, deadline {fm['deadline']}. Draft it."),
        _turn('E', 'continue', "Condense to three bullets."),
    ]
    turns += [                                                   # B1 invoice dep on A (2)
        _turn('B1', 'dep_branch', target='A', message=
              f"Billing time — for the report work from earlier, issue an invoice "
              f"from that same company to {inv['client']}: ${inv['amount']:,}, "
              f"number {inv['invnum']}, due {inv['due']}. Confirm details first."),
        _turn('B1', 'continue',
              "Draft the invoice text block with issuer company, client, amount, "
              "number and due date.",
              probe={'kind': 'dependency', 'task': 'B1', 'topics_before': 5,
                     'groups': [
                  {'name': 'issuer_from_parent', 'any_of': [fa['company']]},
                  {'name': 'client', 'any_of': [inv['client']]},
                  {'name': 'amount', 'any_of': _num_variants(inv['amount'])},
              ]}),
    ]
    turns.append(_turn('B', 'return', target='B', message=
        "Back to the blog post from earlier — what was the working title and the "
        "target audience? Then give me a closing paragraph for it.",
        probe={'kind': 'return_fidelity', 'task': 'B', 'topics_before': 6,
               'groups': [
            {'name': 'title', 'any_of': [fb['title']]},
            {'name': 'audience', 'any_of': [fb['audience']]},
        ]}))
    turns.append(_turn('D', 'return', target='D', message=
        "And on the trip — remind me of the hotel and the budget, then compress "
        "the itinerary to five lines.",
        probe={'kind': 'return_fidelity', 'task': 'D', 'topics_before': 6,
               'groups': [
            {'name': 'hotel', 'any_of': [ft['hotel']]},
            {'name': 'budget', 'any_of': _num_variants(ft['budget'])},
        ]}))
    all_facts = {'A': fa, 'B': fb, 'C': fc, 'D': ft, 'E': fm, 'B1': inv}
    turns += _session_end_probes(all_facts, ['A', 'C'])
    return turns, all_facts


def s6_longhaul(rng, run_tag):
    """Realistic long session: 10 topics, natural tool use (codebase tasks in
    the agent's workspace generate genuine tool-result bloat), returns and
    probes throughout. No chat-only preamble. Tests the regime where linear
    context accumulates tool noise across many unrelated topics."""
    fa = _report_facts(rng, run_tag)
    fb = _blog_facts(rng, run_tag)
    fc = _faq_facts(rng, run_tag)
    ft = _trip_facts(rng, run_tag)
    fm = _meeting_facts(rng, run_tag)
    inv = _invoice_facts(rng, run_tag, 1)

    turns = []
    # A: weekly report (chat, injected facts)
    turns += _report_turns(fa)
    # B: codebase — port config (tools; prescribed file so probes are stable)
    turns += [
        _turn('B', 'indep_branch',
              "Different thing — a quick codebase question about this workspace: "
              "open config.py and tell me the default PORT and HOST values."),
        _turn('B', 'continue', "How does that file load the .env file? Briefly."),
        _turn('B', 'continue', "Summarize config.py's overall structure in five bullets."),
    ]
    # C: blog post (chat) — relabel from the builder's hardcoded 'B'
    blog = _blog_turns(fb)[:3]
    for t in blog:
        t['task'] = 'C'
    turns += blog
    # return to A + probe (3 topics so far)
    turns.append(_return_to_report_probe(fa, n_topics_before=3))
    turns += _report_finish_turns(fa)
    # D: codebase — routes (tools)
    turns += [
        _turn('D', 'indep_branch',
              "Another code question: in routes/agents.py, how many routes are "
              "defined? Use grep on the @agents_bp.route decorators and give me "
              "the count."),
        _turn('D', 'continue',
              "Which of those routes handle chat specifically? List the paths."),
        _turn('D', 'continue', "Any POST routes among them? Name three."),
    ]
    # E: invoice dep-branch on A
    turns += [
        _turn('E', 'dep_branch', target='A', message=
              f"Billing time — for the report work from earlier, issue an invoice "
              f"from that same company to {inv['client']}: ${inv['amount']:,}, "
              f"number {inv['invnum']}, due {inv['due']}. Confirm the details first, "
              "in chat."),
        _turn('E', 'continue',
              "Draft the invoice text block with issuer company, client, amount, "
              "number and due date.",
              probe={'kind': 'dependency', 'task': 'E', 'topics_before': 5,
                     'groups': [
                  {'name': 'issuer_from_parent', 'any_of': [fa['company']]},
                  {'name': 'client', 'any_of': [inv['client']]},
                  {'name': 'amount', 'any_of': _num_variants(inv['amount'])},
              ]}),
    ]
    # F: trip (chat)
    turns += [
        _turn('F', 'indep_branch',
              f"Now something personal: plan a {ft['days']}-day trip to {ft['city']}. "
              f"Budget ${ft['budget']:,}, staying at {ft['hotel']}. Day-by-day bullets."),
        _turn('F', 'continue', "Refine days 1-2 with time slots."),
        _turn('F', 'continue', "Add a short packing list."),
    ]
    # return to B + probe (6 topics)
    turns.append(_turn('B', 'return', target='B', message=
        "Back to that config question from earlier — from our conversation only: "
        "which file did we inspect for the server config, and what default port "
        "did it show?",
        probe={'kind': 'return_fidelity', 'task': 'B', 'topics_before': 6,
               'groups': [
            {'name': 'file', 'any_of': ['config.py']},
            {'name': 'port', 'any_of': ['8080']},
        ]}))
    # G: meeting notes (chat)
    turns += [
        _turn('G', 'indep_branch',
              f"Write up meeting notes: attendees {', '.join(fm['attendees'])}. "
              f"Decision: {fm['decision']}. {fm['owner']} owns follow-up, "
              f"deadline {fm['deadline']}."),
        _turn('G', 'continue', "Condense to three bullets."),
    ]
    # H: codebase — CLI (tools)
    turns += [
        _turn('H', 'indep_branch',
              "Code again: look at cli/commands.py — how do you start the server, "
              "and is there a daemon mode flag?"),
        _turn('H', 'continue', "What other subcommands does the CLI expose? Just names."),
        _turn('H', 'continue', "Show the exact command to start on port 9090."),
    ]
    # return to D + probe (8 topics)
    turns.append(_turn('D', 'return', target='D', message=
        "Earlier we counted routes in one of the files — which file was it, "
        "and roughly how many routes did you find? From memory of this chat.",
        probe={'kind': 'return_fidelity', 'task': 'D', 'topics_before': 8,
               'groups': [
            {'name': 'file', 'any_of': ['routes/agents.py', 'agents.py']},
        ]}))
    # I: product FAQ (chat) — relabel from the builder's hardcoded 'C'
    faq = _faq_turns(fc)
    for t in faq:
        t['task'] = 'I'
        if t['probe']:
            t['probe']['task'] = 'I'
    turns += faq
    # J: codebase — defaults dir (tools)
    turns += [
        _turn('J', 'indep_branch',
              "One more workspace question: list the files in the defaults/ "
              "directory."),
        _turn('J', 'continue', "Pick any one of them and summarize its purpose in two lines."),
    ]
    # late returns + probes (10 topics)
    turns.append(_turn('F', 'return', target='F', message=
        "Back to the trip — hotel and total budget again, from this conversation? "
        "Then compress the plan to five lines.",
        probe={'kind': 'return_fidelity', 'task': 'F', 'topics_before': 10,
               'groups': [
            {'name': 'hotel', 'any_of': [ft['hotel']]},
            {'name': 'budget', 'any_of': _num_variants(ft['budget'])},
        ]}))
    turns.append(_turn('C', 'return', target='C', message=
        "And the blog post — working title and audience? Then write its closing "
        "paragraph.",
        probe={'kind': 'return_fidelity', 'task': 'C', 'topics_before': 10,
               'groups': [
            {'name': 'title', 'any_of': [fb['title']]},
            {'name': 'audience', 'any_of': [fb['audience']]},
        ]}))
    # session-end recap probes
    all_facts = {'A': fa, 'B': None, 'C': fb, 'D': None, 'E': inv,
                 'F': ft, 'G': fm, 'H': None, 'I': fc, 'J': None}
    turns.append(_turn('A', 'return', target='A', message=
        "Wrap-up recap, from this conversation only: the weekly report — company, "
        "revenue figure, and draft file path?",
        probe={'kind': 'session_end', 'task': 'A', 'topics_before': 10, 'groups': [
            {'name': 'company', 'any_of': [fa['company']]},
            {'name': 'revenue', 'any_of': _num_variants(fa['revenue'])},
            {'name': 'path', 'any_of': [fa['path']]},
        ]}))
    turns.append(_turn('G', 'return', target='G', message=
        "The meeting notes — who owns the follow-up and what's the deadline?",
        probe={'kind': 'session_end', 'task': 'G', 'topics_before': 10, 'groups': [
            {'name': 'owner', 'any_of': [fm['owner'].split()[0]]},
            {'name': 'deadline', 'any_of': [fm['deadline']]},
        ]}))
    turns.append(_turn('I', 'return', target='I', message=
        "And the FAQ product — name, version and price?",
        probe={'kind': 'session_end', 'task': 'I', 'topics_before': 10, 'groups': [
            {'name': 'product', 'any_of': [fc['product']]},
            {'name': 'version', 'any_of': [fc['version']]},
            {'name': 'price', 'any_of': _num_variants(fc['price'])},
        ]}))
    return turns, all_facts


SAMPLE_TITLES = ['Harvest Notes', 'Quiet Launch', 'Copper Sky', 'Winter Draft']
SAMPLE_WORDS = ['luminous', 'granular', 'weightless', 'crisp']
LUNCH_PLACES = ['Fern & Rye', 'Cedar Canteen', 'The Brass Spoon', 'Nori House']
REMINDERS = ['submit the quarterly tax report', 'renew the domain registration',
             'send the invoice follow-up email', 'book the dentist appointment']


def s7_crosstool(rng, run_tag):
    """Five-topic cross-task test with real tool work on Gemma4-12B:
    A: coding a todo CLI, B: daily activity (chat facts), C: codebase research,
    D: a second, unrelated coding project, E: reminder via scheduler tools.
    Measures cross-task effectiveness when tasks are tool-heavy and interleaved."""
    person = rng.choice(PEOPLE)
    place = rng.choice(LUNCH_PLACES)
    standup = f"{rng.choice([9, 10])}:{rng.choice(['00', '15', '30'])} AM"
    gym = f"{rng.choice([5, 6, 7])} PM"
    reminder = rng.choice(REMINDERS)
    rem_time = f"{rng.choice([8, 9, 10, 11])}:00 AM"
    title = rng.choice(SAMPLE_TITLES)
    word = rng.choice(SAMPLE_WORDS)
    tl = f"bench_ws/taskloop_{run_tag}"     # project dirs salted per run
    mg = f"bench_ws/mdglow_{run_tag}"

    turns = [
        # A: todo CLI project
        _turn('A', 'indep_branch',
              f"New coding project. In the workspace, create the directory {tl}/ "
              f"containing taskloop.py — a CLI todo app with three commands: add, "
              f"list, done. Tasks are stored in tasks.json in that same directory. "
              "Write the code and show me the main structure."),
        _turn('A', 'continue',
              "Run it: add two tasks 'buy milk' and 'ship report', then list them. "
              "Show me the actual output."),
        _turn('A', 'continue',
              "Add a --priority flag to the add command (high/normal/low, default "
              "normal), shown in list output. Verify by running it."),
        # B: daily activity
        _turn('B', 'indep_branch',
              f"Different thing — help me organize my day. Standup at {standup}, "
              f"gym at {gym}, lunch with {person} at {place}. Draft my day plan."),
        _turn('B', 'continue', "Compress it into a tight schedule table."),
        # return A + probe
        _turn('A', 'return', target='A', message=
              "Back to the todo project. Quick check from our conversation only: "
              "what's the storage filename, and what were the three original "
              "commands? Then add a 'clear' command that empties the storage.",
              probe={'kind': 'return_fidelity', 'task': 'A', 'topics_before': 2,
                     'groups': [
                  {'name': 'storage', 'any_of': ['tasks.json']},
                  {'name': 'cmd_add', 'any_of': ['add']},
                  {'name': 'cmd_list', 'any_of': ['list']},
                  {'name': 'cmd_done', 'any_of': ['done']},
              ]}),
        # C: research in the repo
        _turn('C', 'indep_branch',
              "Research task: I want to understand Evonic's plugin system. Look at "
              "plugins/token_monitor/ in the workspace — read plugin.json and "
              "handler.py, then explain how the plugin subscribes to events and "
              "which event it listens to."),
        _turn('C', 'continue',
              "Where does that plugin store its data? Find the database path and "
              "the table name."),
        _turn('C', 'continue', "Summarize the plugin's architecture in five bullets."),
        # D: second coding project
        _turn('D', 'indep_branch',
              f"Another new coding project, unrelated to the todo app: create "
              f"{mg}/mdglow.py — a tiny markdown-to-HTML converter with two "
              "functions, convert_heading() and convert_bold(). Write it."),
        _turn('D', 'continue',
              f"Test it: run the converter on '# {title}' and 'this is **bold**' "
              "and show the HTML output."),
        # E: reminder via scheduler
        _turn('E', 'indep_branch',
              f"Set a reminder for me: \"{reminder}\" tomorrow at {rem_time}. "
              "Use the scheduler."),
        _turn('E', 'continue', "List my schedules to confirm it's registered."),
        # returns + probes
        _turn('B', 'return', target='B', message=
              "Back to my day — from this conversation only: what time is the gym, "
              "and who am I having lunch with, where? Then mark standup as done.",
              probe={'kind': 'return_fidelity', 'task': 'B', 'topics_before': 5,
                     'groups': [
                  {'name': 'gym', 'any_of': [gym]},
                  {'name': 'person', 'any_of': [person.split()[0]]},
                  {'name': 'place', 'any_of': [place]},
              ]}),
        _turn('C', 'return', target='C', message=
              "On the research from earlier — from memory of this chat: which "
              "plugin did we study, which event does it subscribe to, and what's "
              "its data table called?",
              probe={'kind': 'return_fidelity', 'task': 'C', 'topics_before': 5,
                     'groups': [
                  {'name': 'plugin', 'any_of': ['token_monitor', 'token monitor']},
                  {'name': 'event', 'any_of': ['llm_usage']},
                  {'name': 'table', 'any_of': ['token_usage']},
              ]}),
        _turn('A', 'return', target='A', message=
              "Comparing my two coding projects, from memory only: which file "
              "stores the todo data in the first project, and what are the two "
              "converter functions in the second?",
              probe={'kind': 'return_fidelity', 'task': 'A', 'topics_before': 5,
                     'groups': [
                  {'name': 'storage', 'any_of': ['tasks.json']},
                  {'name': 'fn_heading', 'any_of': ['convert_heading']},
                  {'name': 'fn_bold', 'any_of': ['convert_bold']},
              ]}),
        _turn('D', 'return', target='D', message=
              f"Extend mdglow: add convert_italic() for *text* and verify it on "
              f"'*{word}*'. Show the output."),
        _turn('E', 'return', target='E', message=
              "About that reminder — what did I ask to be reminded of, and at what "
              "time? Now cancel it and confirm the cancellation.",
              probe={'kind': 'return_fidelity', 'task': 'E', 'topics_before': 5,
                     'groups': [
                  {'name': 'text', 'any_of': [reminder]},
                  {'name': 'time', 'any_of': [rem_time]},
              ]}),
        _turn('A', 'return', target='A', message=
              "Final check on taskloop: run it once more — add 'final task', then "
              "list. Show the output."),
        _turn('A', 'return', target='A', message=
              "Wrap-up recap, from this conversation only: (1) the todo app's "
              "storage file, (2) the event the plugin we researched listens to, "
              "(3) who I had lunch with today.",
              probe={'kind': 'session_end', 'task': 'A', 'topics_before': 5,
                     'groups': [
                  {'name': 'storage', 'any_of': ['tasks.json']},
                  {'name': 'event', 'any_of': ['llm_usage']},
                  {'name': 'person', 'any_of': [person.split()[0]]},
              ]}),
    ]
    facts = {'person': person, 'place': place, 'standup': standup, 'gym': gym,
             'reminder': reminder, 'rem_time': rem_time, 'title': title,
             'word': word, 'taskloop_dir': tl, 'mdglow_dir': mg}
    return turns, facts


_BIG_FILES = [
    'routes/agents.py', 'backend/agent_runtime/llm_loop.py',
    'backend/agent_runtime/runtime.py', 'backend/agent_runtime/context.py',
    'backend/agent_runtime/cmp/store.py', 'models/chat.py',
    'backend/tools/registry.py', 'backend/agent_runtime/cmp/detector.py',
]


def s8_megamarathon(rng, run_tag):
    """COMBINED long-haul variant: dependency DEPTH + TOOL-HEAVY content +
    many tasks. Three A→B→C(/B) dependency chains build the path hierarchy,
    while interspersed research tasks read LARGE real source files — their
    long tool outputs stay in a full-history baseline's transcript (bloating
    it past the small model's degradation threshold) but are offloaded from a
    CMP session after each path switch. Fact-bearing topics are probed; the
    file-reading fillers exist to grow context. ~20 nodes > MAX_PRESERVED so
    early paths archive; late probes target archived A-level facts, deep
    B/C-level facts, and C-level returns needing ancestor restoration."""
    # ── seeded facts per topic (unique so probes have deterministic truth) ──
    person = rng.choice(PEOPLE); place = rng.choice(LUNCH_PLACES)
    gym = f"{rng.choice([5,6,7])} PM"
    reminder = rng.choice(REMINDERS); rem_time = f"{rng.choice([8,9,10,11])} AM"
    trip = _trip_facts(rng, run_tag); meet = _meeting_facts(rng, run_tag)
    faq = _faq_facts(rng, run_tag)
    tl = f"bench_ws/taskloop_{run_tag}"; mg = f"bench_ws/mdglow_{run_tag}"
    ex = f"bench_ws/expense_{run_tag}"
    color = rng.choice(['teal', 'amber', 'indigo', 'crimson'])
    pkg = f"mdglow-cli-{run_tag}"

    codename = rng.choice(['Falcon', 'Kestrel', 'Marlin', 'Cobalt'])  # NIAH needle #1
    T = []
    # === chain 1: A(todo) -> B(tests, dep A) -> C(CI, dep B) ===
    T += [_turn('A', 'indep_branch',
                f"New coding project (internal codename {codename}): create "
                f"{tl}/taskloop.py — a CLI todo app with commands add, list, done, "
                "storing to tasks.json. Write it."),
          _turn('A', 'continue', "Run it: add 'buy milk', then list. Show output.")]
    T += [_turn('Atest', 'dep_branch', target='A', message=
                "Now, building on that todo app, write a SEPARATE test file "
                "test_taskloop.py that imports and tests its add, list and done "
                "functions. This is a new file that depends on the todo app.")]
    T += [_turn('Aci', 'dep_branch', target='Atest', message=
                "Based on those tests, create a CI config .github/workflows/ci.yml "
                "that runs test_taskloop.py with pytest. It depends on the tests.")]
    # 3 SIBLING branches on the todo app (A): tests(Atest), docs, docker — all
    # dep on A, three children at the same level under one parent.
    T += [_turn('Adocs', 'dep_branch', target='A', message=
                "Also for that todo app, write a SEPARATE README.md documenting its "
                "add/list/done commands. New file, depends on the todo app.")]
    T += [_turn('Adocker', 'dep_branch', target='A', message=
                "And a Dockerfile for the todo app using base image "
                f"python:3.11-slim. New file, depends on the todo app.")]
    # === tool-heavy filler: read large real files (bloats a full-history
    #     transcript; CMP offloads them on switch). Not probed — context only. ===
    def _read_filler(label, *files):
        flist = ', '.join(files)
        return _turn(label, 'indep_branch',
                     f"Code review task: read these files in the workspace — {flist} "
                     "— and summarize each one's overall responsibility in 2 bullets.")
    T += [_read_filler('R1', _BIG_FILES[0], _BIG_FILES[1])]
    # === breadth: independent topics ===
    T += [_turn('Daily', 'indep_branch',
                f"Different thing — organize my day: gym at {gym}, lunch with "
                f"{person} at {place}. Draft a schedule.")]
    T += [_turn('Plugin', 'indep_branch',
                "Research: read plugins/token_monitor/plugin.json and handler.py; "
                "tell me which event the plugin subscribes to and its data table.")]
    T += [_turn('PluginDoc', 'dep_branch', target='Plugin', message=
                "From that plugin research, write a SEPARATE docs/token_monitor.md "
                "documenting the event and table. New file, depends on the research.")]
    T += [_read_filler('R2', _BIG_FILES[2], _BIG_FILES[3])]
    # === chain 2: D(mdglow) -> B(wrapper, dep D) -> C(packaging, dep B) ===
    T += [_turn('D', 'indep_branch',
                f"Another coding project: create {mg}/mdglow.py with functions "
                "convert_heading() and convert_bold(). Write and test it.")]
    T += [_turn('Dcli', 'dep_branch', target='D', message=
                "Building on mdglow, write a SEPARATE command-line wrapper cli.py "
                "that imports its convert_heading and convert_bold to process a "
                "file. New file, depends on mdglow.")]
    T += [_turn('Dpkg', 'dep_branch', target='Dcli', message=
                f"Now package that wrapper: add a setup.py naming the installable "
                f"tool '{pkg}'. It depends on the cli.py wrapper.")]
    # CHECKPOINT 1 (~mid-early): recall facts established in topic 1 (todo),
    # sampling accuracy partway through the session for the degradation curve.
    T.append(_turn('A', 'return', target='A', message=
        "Quick memory check from earlier in this chat: the internal codename of "
        "the todo project, and what file it stores tasks in?",
        probe={'kind': 'checkpoint', 'task': 'A', 'topics_before': 8, 'groups': [
            {'name': 'codename', 'any_of': [codename]},
            {'name': 'storage', 'any_of': ['tasks.json']}]}))
    # === 5-HOP deep chain: data pipeline A→B→C→D→E (each dep on the previous).
    #     Deeper than RESTORE_MAX_HOPS(3), so the deepest node's top ancestors
    #     fall outside the restoration horizon — recoverable only via recall /
    #     read_transcript, not automatic ancestor pinning. ===
    pipe = f"bench_ws/pipeline_{run_tag}"
    region = rng.choice(['NW-42', 'SE-17', 'MW-88', 'NE-23'])   # needle in the root
    T += [_turn('Praw', 'indep_branch',
                f"New data project: create {pipe}/raw.py that generates sample sales "
                f"rows to sales_raw.csv, tagging every row with region code {region}.")]
    T += [_turn('Pclean', 'dep_branch', target='Praw', message=
                "Building on that, write clean.py that reads sales_raw.csv and writes "
                "a deduped sales_clean.csv. Depends on the raw generator.")]
    T += [_turn('Pagg', 'dep_branch', target='Pclean', message=
                "Now aggregate.py that reads sales_clean.csv and computes monthly "
                "totals to sales_monthly.csv. Depends on the cleaner.")]
    T += [_turn('Prpt', 'dep_branch', target='Pagg', message=
                "Then report.py that formats sales_monthly.csv into a text report. "
                "Depends on the aggregator.")]
    T += [_turn('Pdash', 'dep_branch', target='Prpt', message=
                "Finally dashboard.py that renders that report as dashboard.html. "
                "Depends on the report.")]
    T += [_read_filler('R3', _BIG_FILES[4], _BIG_FILES[5])]
    # === more breadth (push node count past the preserved cap) ===
    T += [_turn('Remind', 'indep_branch',
                f"Set a reminder: \"{reminder}\" tomorrow at {rem_time}. Use the scheduler.")]
    T += [_turn('Trip', 'indep_branch',
                f"Plan a {trip['days']}-day trip to {trip['city']}, staying at "
                f"{trip['hotel']}, budget ${trip['budget']:,}. Bullets.")]
    T += [_turn('TripPack', 'dep_branch', target='Trip', message=
                "Based on that trip plan, make a SEPARATE packing checklist for it. "
                "Depends on the trip.")]
    T += [_turn('Meet', 'indep_branch',
                f"Meeting notes: attendees {', '.join(meet['attendees'])}; decision: "
                f"{meet['decision']}; {meet['owner']} owns follow-up, due {meet['deadline']}.")]
    T += [_read_filler('R4', _BIG_FILES[6], _BIG_FILES[7])]
    T += [_turn('Config', 'indep_branch',
                "Code question: open config.py and tell me the default PORT and HOST.")]
    T += [_turn('ConfigEnv', 'dep_branch', target='Config', message=
                "From those config defaults, write a SEPARATE .env.example template. "
                "New file, depends on the config findings.")]
    # CHECKPOINT 2 (~mid-late): recall facts from the daily-plan and plugin
    # topics (established earlier), sampling accuracy deeper into the session.
    T.append(_turn('Daily', 'return', target='Daily', message=
        "Quick memory check from earlier: what time was my gym, and which event "
        "does the plugin we researched subscribe to?",
        probe={'kind': 'checkpoint', 'task': 'Daily', 'topics_before': 16, 'groups': [
            {'name': 'gym', 'any_of': [gym]},
            {'name': 'plugin_event', 'any_of': ['llm_usage']}]}))
    T += [_turn('FAQ', 'indep_branch',
                f"Draft a customer FAQ for product {faq['product']}, version "
                f"{faq['version']}, price ${faq['price']}. First two Q&As.")]
    # === 5 NEW topics (ApiSvc→spec→sdk chain, DbSchema→migration chain,
    #     Newsletter→promo chain, plus Survey and Logo) ===
    api = f"bench_ws/apisvc_{run_tag}"; sdk_name = f"orders-sdk-{run_tag}"
    port2 = rng.choice([7000, 7100, 7200, 7300])   # distinctive fact
    tables = rng.choice(['users+orders', 'accounts+ledger', 'items+carts'])
    T += [_turn('ApiSvc', 'indep_branch',
                f"New coding project: create {api}/service.py, a REST API for orders "
                f"that listens on port {port2}. Write it."),
          _turn('ApiSpec', 'dep_branch', target='ApiSvc', message=
                "Building on that API, write a SEPARATE openapi.yaml spec describing "
                "its endpoints. New file, depends on the service."),
          _turn('ApiSdk', 'dep_branch', target='ApiSpec', message=
                f"From that spec, generate a SEPARATE Python client SDK package named "
                f"'{sdk_name}'. Depends on the spec.")]
    T += [_turn('DbSchema', 'indep_branch',
                f"New task: write schema.sql defining the {tables} tables for the "
                "orders database."),
          _turn('DbMig', 'dep_branch', target='DbSchema', message=
                "From that schema, write a SEPARATE 0001_init.sql migration script. "
                "Depends on the schema.")]
    T += [_turn('Newsletter', 'indep_branch',
                "Draft a short product newsletter announcing our Q3 release."),
          _turn('NewsPromo', 'dep_branch', target='Newsletter', message=
                "From that newsletter, write a SEPARATE 3-tweet social promo thread. "
                "Depends on the newsletter.")]
    T += [_turn('Survey', 'indep_branch',
                "Design a 5-question customer satisfaction survey.")]
    T += [_turn('Logo', 'indep_branch',
                "Write a one-paragraph logo design brief for a coffee brand.")]
    # CHECKPOINT 3 (~late): recall the pipeline-root region needle and the SDK
    # name — deep facts sampled near the end for the degradation curve.
    T.append(_turn('Praw', 'return', target='Praw', message=
        "Quick memory check from earlier: what region code were the raw sales "
        "rows tagged with, and what did we name the orders API client SDK?",
        probe={'kind': 'checkpoint', 'task': 'Praw', 'topics_before': 24, 'groups': [
            {'name': 'region', 'any_of': [region]},
            {'name': 'sdk_name', 'any_of': [sdk_name]}]}))
    # === chain 3: M(expense) -> B(report, dep M) ===
    T += [_turn('M', 'indep_branch',
                f"Coding project: create {ex}/expenses.py storing rows to "
                f"expenses.csv, with a {color} summary banner. Write it."),
          _turn('Mrpt', 'dep_branch', target='M', message=
                "From that expense tracker's data, generate a SEPARATE "
                "monthly_summary.py that reads expenses.csv and totals it. "
                "Depends on the expense tracker.")]

    # === TWO continuous coding tasks, developed in INTERLEAVED deep chains
    #     (Auth service and Scraper), simulating ongoing parallel dev. Each
    #     grows 4 levels deep (A→B→C→D); the two are woven together so the
    #     agent context-switches between them repeatedly. ===
    rate = rng.choice([60, 100, 120, 200])          # distinctive Auth fact
    domain = rng.choice(['catalog.example', 'feed.example', 'shop.example'])  # Scraper fact
    aw = f"bench_ws/authsvc_{run_tag}"; sw = f"bench_ws/scraper_{run_tag}"
    T += [_turn('AuthSvc', 'indep_branch',
                f"Continuous coding task 1 — create {aw}/auth_service.py with a "
                "login() function returning a session token. Write it."),
          _turn('AuthTok', 'dep_branch', target='AuthSvc', message=
                "Building on auth_service, add a SEPARATE token_utils.py that issues "
                "JWT tokens for login(). Depends on the auth service.")]
    # SIBLINGS on the auth root: tests + config alongside the deep feature chain
    T += [_turn('AuthTests', 'dep_branch', target='AuthSvc', message=
                "Also for the auth service, add a SEPARATE test_auth.py testing "
                "login(). New file, depends on the auth service."),
          _turn('AuthConf', 'dep_branch', target='AuthSvc', message=
                "And a SEPARATE auth_config.py holding the auth settings. New file, "
                "depends on the auth service.")]
    T += [_turn('Scraper', 'indep_branch',
                f"Continuous coding task 2 — create {sw}/scraper.py that fetches "
                f"pages from {domain}. Write it."),
          _turn('ScrParse', 'dep_branch', target='Scraper', message=
                "Building on the scraper, add a SEPARATE parser.py that extracts "
                "records from the fetched pages. Depends on the scraper.")]
    # SIBLINGS on the scraper root: tests + a logging component
    T += [_turn('ScrTests', 'dep_branch', target='Scraper', message=
                "Also for the scraper, add a SEPARATE test_scraper.py. New file, "
                "depends on the scraper."),
          _turn('ScrLog', 'dep_branch', target='Scraper', message=
                "And a SEPARATE logging_config.py for the scraper. New file, "
                "depends on the scraper.")]
    T += [_turn('AuthMw', 'dep_branch', target='AuthTok', message=
                "Back on task 1: add a SEPARATE middleware.py that validates those "
                "JWT tokens on each request. Depends on the token utils.")]
    T += [_turn('ScrStore', 'dep_branch', target='ScrParse', message=
                "Back on task 2: add a SEPARATE store.py that saves parsed records "
                "to scraped.db. Depends on the parser.")]
    T += [_turn('AuthRl', 'dep_branch', target='AuthMw', message=
                f"Task 1 again: add a SEPARATE rate_limiter.py that the middleware "
                f"calls, capping requests at {rate} per minute. Depends on middleware.")]
    T += [_turn('ScrExport', 'dep_branch', target='ScrStore', message=
                "Task 2 again: add a SEPARATE export.py that dumps scraped.db to a "
                "CSV file. Depends on the store.")]

    # FOLLOW-UP PERSISTENCE test: reference an offloaded path (the plugin
    # research) WITHOUT switching — turn 1 pins it — then a follow-up next turn
    # that needs the same fact while staying on the scraper task. Measures
    # whether the pinned card PERSISTED (sticky pin + promotion) vs vanished
    # after the referencing turn (transient pin).
    T += [_turn('ScrExport', 'continue', message=
                "Quick aside, staying on the scraper task: what event did the "
                "token_monitor plugin we researched earlier subscribe to?")]
    T.append(_turn('ScrExport', 'continue', message=
        "Good — now, still on the scraper, add a one-line comment at the top of "
        "scraper.py noting that same plugin event name.",
        probe={'kind': 'followup_persist', 'task': 'ScrExport', 'topics_before': 34, 'groups': [
            {'name': 'event_persisted', 'any_of': ['llm_usage']}]}))

    # ── probes: archived A-level, deep B/C/E-level, ancestor restore, siblings,
    #     and pure NIAH needles (planted once early, probed only now) ──────────
    NT = 34  # ~topics before the probe block
    T.append(_turn('A', 'return', target='A', message=
        "Back to the very first coding project — from this conversation only: "
        "what file does the todo app store tasks in, and its three commands? "
        "Then add a 'clear' command.",
        probe={'kind': 'return_fidelity', 'task': 'A', 'topics_before': NT, 'groups': [
            {'name': 'storage', 'any_of': ['tasks.json']},
            {'name': 'cmd_add', 'any_of': ['add']},
            {'name': 'cmd_done', 'any_of': ['done']}]}))
    T.append(_turn('Aci', 'return', target='Aci', message=
        "The CI setup for the todo tests — which workflow file did we create, "
        "what test runner does it use, and which test file does it run? "
        "From this conversation.",
        probe={'kind': 'dependency', 'task': 'Aci', 'topics_before': NT, 'groups': [
            {'name': 'workflow', 'any_of': ['ci.yml', '.github/workflows/ci.yml']},
            {'name': 'runner', 'any_of': ['pytest']},
            {'name': 'testfile_from_parent', 'any_of': ['test_taskloop.py', 'test_taskloop']}]}))
    # SIBLING probe: Adocker must be distinct from its siblings Atest / Adocs
    T.append(_turn('Adocker', 'return', target='Adocker', message=
        "The Docker setup for the todo app — what base image did the Dockerfile "
        "use? From this conversation.",
        probe={'kind': 'sibling', 'task': 'Adocker', 'topics_before': NT, 'groups': [
            {'name': 'base_image', 'any_of': ['python:3.11-slim']}]}))
    T.append(_turn('Dpkg', 'return', target='Dpkg', message=
        "The packaging for the markdown converter — what did we name the "
        "installable tool, and which two convert functions does its wrapper use? "
        "From this conversation.",
        probe={'kind': 'dependency', 'task': 'Dpkg', 'topics_before': NT, 'groups': [
            {'name': 'pkg_name', 'any_of': [pkg]},
            {'name': 'fn_heading_from_ancestor', 'any_of': ['convert_heading']},
            {'name': 'fn_bold_from_ancestor', 'any_of': ['convert_bold']}]}))
    # DEEP chain 2 return: ApiSdk (C-level) — the SDK name + the port from its
    # A-level grandparent ApiSvc (2 hops up)
    T.append(_turn('ApiSdk', 'return', target='ApiSdk', message=
        "The orders API client SDK — what did we name the SDK package, and what "
        "port does the underlying API service listen on? From this conversation.",
        probe={'kind': 'deep_ancestor', 'task': 'ApiSdk', 'topics_before': NT, 'groups': [
            {'name': 'sdk_name', 'any_of': [sdk_name]},
            {'name': 'port_from_grandparent', 'any_of': [str(port2)]}]}))
    # DEEP 5-hop return: Pdash (E-level) + a fact from Praw 4 hops up (beyond the
    # h=3 restoration horizon → recoverable only via recall/read_transcript)
    T.append(_turn('Pdash', 'return', target='Pdash', message=
        "The data pipeline — what HTML file does the dashboard render to, and "
        "what region code were the raw sales rows tagged with at the very start "
        "of that pipeline? From this conversation.",
        probe={'kind': 'deep_ancestor', 'task': 'Pdash', 'topics_before': NT, 'groups': [
            {'name': 'dashboard', 'any_of': ['dashboard.html']},
            {'name': 'region_needle_from_root', 'any_of': [region]}]}))
    # CONTINUOUS-TASK deep returns: the D-level tail of each interleaved chain,
    # needing a fact from its A-level root (Auth: rate + login; Scraper: export
    # + scraped.db from ancestor).
    T.append(_turn('AuthRl', 'return', target='AuthRl', message=
        "On the auth service task — what per-minute request cap did the rate "
        "limiter use, and what function does the whole auth flow start from? "
        "From this conversation.",
        probe={'kind': 'deep_ancestor', 'task': 'AuthRl', 'topics_before': NT, 'groups': [
            {'name': 'rate_limit', 'any_of': [str(rate)]},
            {'name': 'login_from_root', 'any_of': ['login']}]}))
    T.append(_turn('ScrExport', 'return', target='ScrExport', message=
        "On the scraper task — which domain did the scraper fetch from, and what "
        "database file does the store write to? From this conversation.",
        probe={'kind': 'deep_ancestor', 'task': 'ScrExport', 'topics_before': NT, 'groups': [
            {'name': 'domain_from_root', 'any_of': [domain]},
            {'name': 'db_from_ancestor', 'any_of': ['scraped.db']}]}))
    # SIBLING probe on the auth root's several children (token/tests/config):
    # each must stay a distinct file, not conflated.
    T.append(_turn('AuthConf', 'return', target='AuthConf', message=
        "For the auth service, we made several separate files off it — name the "
        "one that issues JWT tokens, the one that tests login, and the one that "
        "holds settings. From this conversation.",
        probe={'kind': 'sibling', 'task': 'AuthConf', 'topics_before': NT, 'groups': [
            {'name': 'token_file', 'any_of': ['token_utils']},
            {'name': 'test_file', 'any_of': ['test_auth']},
            {'name': 'config_file', 'any_of': ['auth_config']}]}))
    # NIAH: needles planted ONCE, early, never referenced — retrieved only now
    T.append(_turn('A', 'return', target='A', message=
        "Two quick memory checks, from this conversation only: (1) the internal "
        "codename of the todo project, and (2) the city of my trip.",
        probe={'kind': 'niah', 'task': 'A', 'topics_before': NT, 'groups': [
            {'name': 'codename_needle_topic1', 'any_of': [codename]},
            {'name': 'trip_city', 'any_of': [trip['city']]}]}))
    T.append(_turn('A', 'return', target='A', message=
        "Final wrap-up recap, from this conversation only: (1) the todo app's "
        "storage file, (2) the plugin's event, (3) who I had lunch with.",
        probe={'kind': 'session_end', 'task': 'A', 'topics_before': NT, 'groups': [
            {'name': 'storage', 'any_of': ['tasks.json']},
            {'name': 'event', 'any_of': ['llm_usage']},
            {'name': 'person', 'any_of': [person.split()[0]]}]}))
    # MULTI-PROJECT NIAH: summarize ALL coding projects — one distinctive fact
    # per project, spread across the whole session. Accuracy = fraction of the
    # 6 old projects whose key fact is recalled in one synthesis.
    T.append(_turn('A', 'return', target='A', message=
        "To wrap up, summarize every coding project we built today — for EACH "
        "project, give its name and one concrete detail (a filename, function, "
        "port, limit, or storage). Cover them all, from this conversation only.",
        probe={'kind': 'multi_project', 'task': 'A', 'topics_before': NT, 'groups': [
            {'name': 'todo_taskloop', 'any_of': ['tasks.json', 'taskloop']},
            {'name': 'mdglow_converter', 'any_of': ['convert_heading', 'convert_bold', 'mdglow']},
            {'name': 'orders_api', 'any_of': [str(port2), sdk_name]},
            {'name': 'auth_service', 'any_of': [str(rate), 'rate_limiter', 'token_utils']},
            {'name': 'scraper', 'any_of': ['scraped.db', 'parser.py', domain]},
            {'name': 'expenses', 'any_of': ['expenses.csv']}]}))

    facts = {'codename': codename, 'region': region, 'A_storage': 'tasks.json',
             'rate': rate, 'domain': domain,
             'gym': gym, 'person': person, 'place': place, 'event': 'llm_usage',
             'pkg': pkg, 'sdk_name': sdk_name, 'port2': port2, 'trip': trip, 'meet': meet}
    return T, facts


_ENDURANCE_TASKS = [
    "Write a two-line haiku about {t}.",
    "Convert {n} miles to kilometers — just the number and a word.",
    "Suggest three short names for a {t} app.",
    "One quick practical tip about {t}?",
    "Summarize {t} in a single sentence.",
    "Draft a one-line git commit message for a {t} fix.",
    "Give me one surprising fact about {t}.",
    "Write a short to-do note about {t}.",
    "Rename this variable meaningfully: the thing that tracks {t}.",
    "One-sentence pros and cons of {t}.",
]
_ENDURANCE_NOUNS = [
    "caching", "onboarding", "gardening", "invoices", "latency", "coffee",
    "backups", "typography", "commuting", "logging", "hydration", "pagination",
    "sleep", "refactoring", "budgeting", "focus", "testing", "networking",
    "recipes", "posture", "metrics", "deploys", "reading", "stretching",
]
_LANDMARKS = [
    ("flight gate", lambda r: r.choice("ABCDEF") + str(r.randint(1, 40))),
    ("API key", lambda r: r.choice(["XZ", "QP", "RT", "MK"]) + "-" + str(r.randint(1000, 9999))),
    ("budget cap", lambda r: "$" + format(r.randint(3, 15) * 500, ",")),
    ("server IP", lambda r: "10." + str(r.randint(0, 9)) + "." + str(r.randint(0, 20)) + "." + str(r.randint(1, 254))),
    ("meeting slot", lambda r: r.choice(["Mon", "Tue", "Wed", "Thu", "Fri"]) + " " + str(r.choice([9, 10, 2, 3, 4])) + "pm"),
    ("project room", lambda r: r.choice(["Maple", "Cedar", "Birch", "Willow", "Aspen"])),
    ("deadline", lambda r: r.choice(["June", "July", "August", "September"]) + " " + str(r.randint(1, 28))),
    ("service port", lambda r: str(r.randint(6000, 9999))),
    ("confirmation code", lambda r: r.choice(["CF", "BK", "OR"]) + str(r.randint(100000, 999999))),
    ("locker number", lambda r: str(r.randint(100, 999))),
]


def s9_endurance(rng, run_tag):
    """Endurance / real-world profile: a long stream of mostly-NEW short topics
    with only SPARSE reminder-style lookups of old context. Distinctive
    'landmarks' (a gate, an API key, a deadline…) are planted periodically; a
    lookup every ~11 turns recalls a landmark from ~20 turns back (fixed lag,
    so as the session grows the haystack grows but the needle's recency is
    constant — isolating context-SIZE degradation). Runs up to 400 turns; the
    runner stops early when full-history's lookups hit 0 three times running or
    a turn errors (context ceiling). Purpose: find where each approach fails."""
    TARGET = 400
    LAG = 20                       # recall a landmark planted ~this many turns back
    T = []
    landmarks = []                 # (turn_planted, thing, value)
    for i in range(TARGET):
        if i % 6 == 2:             # plant a landmark
            thing, gen = _LANDMARKS[(i // 6) % len(_LANDMARKS)]
            value = gen(rng)
            landmarks.append((i, thing, value))
            T.append(_turn(f'L{i}', 'indep_branch',
                           f"Quick note to remember for later: my {thing} is {value}. "
                           "Acknowledge and we'll move on."))
            continue
        if i % 11 == 7:            # sparse reminder-style lookup
            eligible = [lm for lm in landmarks if i - lm[0] >= LAG]
            if eligible:
                planted, thing, value = eligible[-1]   # closest to the LAG horizon
                T.append(_turn(f'Q{i}', 'return', target=None, message=
                    f"Reminder lookup — from earlier in our chat, what was my {thing}?",
                    probe={'kind': 'lookup', 'task': f'Q{i}', 'topics_before': i,
                           'planted_turn': planted, 'age': i - planted,
                           'groups': [{'name': thing.replace(' ', '_'),
                                       'any_of': [value]}]}))
                continue
        # plain new-topic task (accumulates context)
        noun = _ENDURANCE_NOUNS[i % len(_ENDURANCE_NOUNS)]
        tmpl = _ENDURANCE_TASKS[i % len(_ENDURANCE_TASKS)]
        T.append(_turn(f'N{i}', 'indep_branch',
                       tmpl.format(t=noun, n=rng.randint(2, 60))))
    return T, {'landmarks': landmarks}


_AGE_BANDS = [15, 30, 60, 120, 240]   # recall distances (turns) the tail sweeps
# Distinct two-word codenames so every landmark is UNIQUELY identifiable —
# without this, reusing a fact TYPE (e.g. "service port") across many plants
# makes "what was my service port?" ambiguous and unscoreable. 12x12=144
# combos cover the ~90 landmarks a run plants, each used once.
_LM_ADJ = ['crimson', 'azure', 'golden', 'silver', 'emerald', 'violet',
           'amber', 'coral', 'ivory', 'onyx', 'jade', 'ruby']
_LM_NOUN = ['falcon', 'otter', 'cedar', 'harbor', 'lantern', 'meadow',
            'comet', 'quartz', 'willow', 'anchor', 'maple', 'raven']


def _codename(k):
    return f"{_LM_ADJ[(k // len(_LM_NOUN)) % len(_LM_ADJ)]}-{_LM_NOUN[k % len(_LM_NOUN)]}"


def s10_hybrid(rng, run_tag):
    """Hybrid endurance with an AGE-STRATIFIED recall sweep.

    Phase 1 (~turns 1-90): s8's rich structure — dependency chains, sibling
    fan-outs, deep-ancestor + NIAH + multi-project fidelity probes — with
    distinctive 'landmark' facts seeded through it (planted, not yet probed).

    Phase 2 (tail to 400): a stream of short new topics that keeps planting
    landmarks densely, plus a lookup every ~6 turns that CYCLES through fixed
    recall distances (_AGE_BANDS = 15/30/60/120/240 turns back), probing the
    landmark nearest that distance. This charts accuracy-vs-distance per arm:
    the band where accuracy collapses IS that arm's recall wall (windowed
    baseline ≈ its 50-msg window; CMP ≈ its eviction horizon; true full-history
    holds until the context ceiling hard-fails). Deep bands only sample once
    the session is long enough, and the deepest reach back into the s8 phase
    (true deep-NIAH at scale). Each lookup records age + age_band for the curve.

    Up to 400 turns; the runner stops an arm on a hard timeout (context
    ceiling) or on N consecutive zero-accuracy lookups (set high for this
    scenario so expected deep-band misses don't stop it early — the curve, not
    an early stop, locates each wall). Facts salted per (salt, schedule, seed).
    """
    TARGET = 400
    s8_turns, s8_facts = s8_megamarathon(rng, run_tag)
    T = []
    landmarks = []                 # (turn_index_in_T, codename, thing, value)
    lm_i = [0]

    def _plant():
        k = lm_i[0]
        lm_i[0] += 1
        thing, gen = _LANDMARKS[k % len(_LANDMARKS)]
        value = gen(rng)
        cn = _codename(k)          # unique per landmark → unambiguous recall
        landmarks.append((len(T), cn, thing, value))
        return _turn(f'L{len(T)}', 'indep_branch',
                     f"Quick note about the {cn} project: its {thing} is {value}. "
                     "Acknowledge and we'll move on.")

    def _lookup_at_age(target):
        """Probe the landmark planted closest to `target` turns ago, keyed by
        its unique codename so the answer is unambiguous. Returns None when the
        session isn't long enough for that depth yet, or no landmark is old
        enough (so the recorded age stays near the band)."""
        want = len(T) - target
        if want < 0 or not landmarks:
            return None
        lm = min(landmarks, key=lambda l: abs(l[0] - want))
        if lm[0] > want + 3:
            return None
        planted, cn, thing, value = lm
        return _turn(f'Q{len(T)}', 'return', target=None,
                     message=f"Reminder lookup — what was the {thing} for the "
                             f"{cn} project?",
                     probe={'kind': 'lookup', 'task': f'Q{len(T)}',
                            'planted_turn': planted, 'age': len(T) - planted,
                            'age_band': target,
                            'groups': [{'name': f'{cn}_{thing}'.replace(' ', '_'),
                                        'any_of': [value]}]})

    def _newtask():
        noun = _ENDURANCE_NOUNS[len(T) % len(_ENDURANCE_NOUNS)]
        tmpl = _ENDURANCE_TASKS[len(T) % len(_ENDURANCE_TASKS)]
        return _turn(f'N{len(T)}', 'indep_branch',
                     tmpl.format(t=noun, n=rng.randint(2, 60)))

    # Phase 1 — s8 structure; seed landmarks through it (no lookups here, so
    # s8's own fidelity probes stay clean). These deep landmarks feed the
    # tail's deepest bands.
    for i, st in enumerate(s8_turns):
        T.append(st)
        if i and i % 6 == 0:
            T.append(_plant())

    # Phase 2 — age-stratified recall sweep to TARGET turns (or the wall).
    band_i = [0]
    j = 0
    while len(T) < TARGET:
        if j % 4 == 1:                          # dense landmarks: a target for
            T.append(_plant())                  # every band at every distance
        elif j % 6 == 3:
            reach = [b for b in _AGE_BANDS if b < len(T) - 2]
            if reach:
                band = reach[band_i[0] % len(reach)]
                band_i[0] += 1
                T.append(_lookup_at_age(band) or _newtask())
            else:
                T.append(_newtask())
        else:
            T.append(_newtask())
        j += 1

    return T[:TARGET], {'landmarks': landmarks, 's8_facts': s8_facts}


_BUILDERS = {
    's1_sequential': s1_sequential,
    's2_single_return': s2_single_return,
    's3_dep_branch': s3_dep_branch,
    's4_oscillation': s4_oscillation,
    's5_marathon': s5_marathon,
    's6_longhaul': s6_longhaul,
    's7_crosstool': s7_crosstool,
    's8_megamarathon': s8_megamarathon,
    's9_endurance': s9_endurance,
    's10_hybrid': s10_hybrid,
}


def build(schedule, seed, salt=''):
    """Build one deterministic scenario. Facts are salted by (salt, schedule,
    seed) so no two runs share probe answers (guards against agent-memory
    leakage across runs — agents may remember facts between sessions)."""
    assert schedule in _BUILDERS, f"unknown schedule {schedule}"
    rng = random.Random(f"cmpbench:{salt}:{schedule}:{seed}")
    tag_salt = ''.join(ch for ch in salt if ch.isalnum())[:6]
    run_tag = f"{schedule.split('_')[0]}{seed}{tag_salt}"
    turns, facts = _BUILDERS[schedule](rng, run_tag)
    if schedule not in NO_GROUND_RULES:
        turns[0]['message'] = GROUND_RULES + turns[0]['message']
    for i, t in enumerate(turns):
        t['idx'] = i
    n_probes = sum(1 for t in turns if t['probe'])
    return {'schedule': schedule, 'seed': seed, 'salt': salt, 'run_tag': run_tag,
            'turns': turns, 'facts': facts, 'n_turns': len(turns),
            'n_probes': n_probes}


if __name__ == '__main__':
    for sch in SCHEDULES:
        for seed in (1, 2):
            sc = build(sch, seed)
            again = build(sch, seed)
            assert [t['message'] for t in sc['turns']] == \
                   [t['message'] for t in again['turns']], "not deterministic!"
            print(f"{sch} seed={seed}: {sc['n_turns']} turns, {sc['n_probes']} probes")
