"""
monitor — Attach condition watchers to background processes, logs, or shell checks.

Thin dispatch onto :mod:`backend.agent_runtime.monitors`, which owns validation,
the persisted schedule and the notification. Background processes are never
watched automatically; this tool is how an agent opts in.
"""


def execute(agent: dict, args: dict) -> dict:
    from backend.agent_runtime import monitors

    agent = agent or {}
    action = args.get('action', 'attach')

    if action == 'attach':
        return monitors.attach(
            agent,
            target=args.get('target') or {},
            when=args.get('when') or {},
            note=args.get('note') or '',
            interval=args.get('interval', 30),
            expires_in=args.get('expires_in', 6 * 60 * 60),
        )

    agent_id = agent.get('agent_id') or agent.get('id') or ''
    if not agent_id:
        return {'error': 'Monitor requires an agent context.'}

    if action == 'list':
        return {'monitors': monitors.list_for_session(
            agent_id, agent.get('session_id') or 'default')}

    if action == 'detach':
        return monitors.detach(agent_id, args.get('monitor_id') or '')

    return {'error': f"Unknown action: {action!r}. Use 'attach', 'list' or 'detach'."}


def test_execute():
    assert 'error' in execute({}, {'action': 'bogus'})
    assert 'error' in execute({}, {'action': 'list'})          # no agent context
    # attach validates before touching the DB
    assert 'error' in execute({'agent_id': 'a'}, {'when': {}})
    assert 'error' in execute({'agent_id': 'a'}, {'when': {'nope': 1}})
    assert 'error' in execute({'agent_id': 'a'}, {'when': {'on_exit': True}})
