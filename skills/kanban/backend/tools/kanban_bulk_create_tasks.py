"""
Kanban bulk create tool — create several tasks on the board in a single call.

Why this exists: when asked to create multiple tasks at once (e.g. a plan of
9B/9C/9D), the model naturally reaches for a single "bulk create" call. Without
this tool it hallucinates a non-existent ``kanban_bulk_create_tasks``, the quality
monitor rejects it, and after the correction cap the model often gives up and
falsely reports a "board/registry error" — so the tasks never get made. Providing
the real tool removes that failure at the source.

It wraps ``kanban_create_task.execute`` for each item so permission checks,
validation, and single-task dependency handling stay identical. Intra-batch
dependencies are supported via ``depends_on_index`` (1-based index of an earlier
task in the same ``tasks`` array), resolved to the real task id once that task is
created — so 9C/9D can depend on 9B without knowing ids in advance.
"""

import importlib.util
import os

_MAX_BATCH = 50
_create_one = None  # cached reference to the single-create execute()


def _single_create():
    """Load the sibling kanban_create_task tool by path (skills/ is not a package)."""
    global _create_one
    if _create_one is None:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "kanban_create_task.py")
        spec = importlib.util.spec_from_file_location("kanban_create_task_sibling", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _create_one = module.execute
    return _create_one


def execute(agent: dict, args: dict) -> dict:
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return {"status": "error", "message": "tasks must be a non-empty array of task objects"}
    if len(tasks) > _MAX_BATCH:
        return {"status": "error", "message": f"cannot create more than {_MAX_BATCH} tasks in one call"}

    create_one = _single_create()

    batch_ids = []   # index -> created task id (or None on failure)
    results = []
    errors = []

    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            msg = "each task must be an object"
            batch_ids.append(None)
            results.append({"status": "error", "index": i, "message": msg})
            errors.append({"index": i, "message": msg})
            continue

        item = dict(t)
        deps = list(item.get("dependencies") or [])

        # Resolve intra-batch dependencies (1-based indices into this array).
        doi = item.pop("depends_on_index", None)
        if doi:
            try:
                for idx in (doi if isinstance(doi, list) else [doi]):
                    ref = batch_ids[int(idx) - 1]
                    if ref is None:
                        raise ValueError(f"depends_on_index {idx} refers to a task that was not created")
                    deps.append(ref)
            except (ValueError, TypeError, IndexError) as e:
                msg = f"invalid depends_on_index: {e}"
                batch_ids.append(None)
                results.append({"status": "error", "index": i, "message": msg, "title": item.get("title")})
                errors.append({"index": i, "message": msg, "title": item.get("title")})
                continue

        if deps:
            item["dependencies"] = deps

        res = create_one(agent, item)
        res = {**res, "index": i}
        results.append(res)
        if res.get("status") == "success":
            batch_ids.append(res.get("task_id"))
        else:
            batch_ids.append(None)
            errors.append({"index": i, "message": res.get("message", "create failed"),
                           "title": item.get("title")})

    created_ids = [tid for tid in batch_ids if tid is not None]
    if not errors:
        status = "success"
    elif created_ids:
        status = "partial"
    else:
        status = "error"

    return {
        "status": status,
        "created_count": len(created_ids),
        "task_ids": created_ids,
        "results": results,
        "errors": errors,
    }
