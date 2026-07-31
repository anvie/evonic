FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def normalize_bool(value, default=True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in FALSE_VALUES:
            return False
        if normalized in TRUE_VALUES:
            return True
    return bool(value)


def message_wrapper_enabled(agent: dict, database=None) -> bool:
    per_agent = agent.get("message_wrapper_enabled")
    if per_agent is not None:
        return normalize_bool(per_agent)
    if database is None:
        from models.db import db as database
    return normalize_bool(database.get_setting("message_wrapper_enabled", "1"))
