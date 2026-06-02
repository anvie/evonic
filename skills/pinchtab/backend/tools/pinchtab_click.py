"""pinchtab_click — click on an element in a browser tab."""

import re

from ._pinchtab_api import _api

_NODEID_RE = re.compile(r'^(?:node)?\d+$', re.IGNORECASE)


def execute(agent: dict, args: dict) -> dict:
    """Click on an element in a browser tab.

    Uses the unified /action endpoint with kind=click.
    The selector supports snapshot ref values (e.g. 'e5', 'e21') or
    CSS selectors (e.g. '#submit-btn', '.nav-link').

    IMPORTANT: Use the `ref` field from pinchtab_snapshot, NOT `nodeId`.

    Args:
        tab_id: ID of the tab.
        selector: Snapshot ref (e.g. 'e5') or CSS selector of the element to click.

    Returns:
        Click result from PinchTab.
    """
    tab_id = args.get("tab_id", "")
    selector = args.get("selector", "")

    if not tab_id:
        return {"error": "tab_id is required."}
    if not selector:
        return {"error": "selector is required. Use the `ref` field (e.g. 'e5') from pinchtab_snapshot, or a CSS selector."}
    if _NODEID_RE.match(selector):
        return {"error": f"'{selector}' looks like a nodeId, not a valid selector. Use the `ref` field from pinchtab_snapshot instead (e.g. 'e5', 'e21')."}

    result = _api("POST", "/action", {
        "tabId": tab_id,
        "kind": "click",
        "selector": selector,
    })
    if "error" in result:
        err = result["error"]
        if "occluded" in err:
            result["hint"] = (
                "The element is behind a modal/overlay. Take a fresh snapshot "
                "and look for the same element inside a 'dialog' role node."
            )
        elif "detached" in err or "not found" in err:
            result["hint"] = (
                "The element no longer exists in the DOM. Take a fresh snapshot "
                "to get updated refs before retrying."
            )
        return result
    return {
        "message": f"Clicked element '{selector}'.",
        "result": result,
    }
