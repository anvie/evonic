"""
copy_status — check the progress of a background portal_copy transfer.
"""


def execute(agent, args: dict) -> dict:
    job_id = (args.get('job_id') or '').strip()
    if not job_id:
        return {'error': "Missing required argument: 'job_id'"}

    from models.db import db
    job = db.get_transfer_job(job_id)
    if not job:
        return {'error': f'No transfer job found with id: {job_id}'}

    # Authorization: only the owning agent can check
    agent_id = (agent or {}).get('id')
    if job.get('agent_id') != agent_id:
        return {'error': 'Access denied — this job belongs to a different agent.'}

    total = job.get('total_bytes', 0)
    transferred = job.get('bytes_transferred', 0)
    pct = round(transferred / total * 100, 1) if total > 0 else 0

    result = {
        'job_id': job_id,
        'status': job['status'],
        'source': job['source_path'],
        'destination': job['dest_path'],
        'total_bytes': total,
        'bytes_transferred': transferred,
        'progress_pct': pct,
    }
    if job.get('error_msg'):
        result['error_msg'] = job['error_msg']
    if job.get('completed_at'):
        result['completed_at'] = job['completed_at']

    return result
