"""
rtk.py — RTK token compression gain endpoint.

Exposes GET /api/rtk/gain — returns cumulative token savings statistics
from the compressor registry.  Equivalent to RTK's `rtk gain` command.

Token counts are approximate (tiktoken cl100k_base estimates).
"""

from flask import Blueprint, jsonify

rtk_bp = Blueprint("rtk", __name__)


@rtk_bp.route("/api/rtk/gain")
def rtk_gain():
    """Return cumulative RTK token savings.

    GET /api/rtk/gain

    Response:
        {
            "pre_tokens": N,       // total tokens before compression
            "post_tokens": M,      // total tokens after compression
            "savings_pct": X.X,    // percentage saved (0.0 if no data)
            "compressions": K      // number of compression events
        }
    """
    try:
        from backend.token_compressor.compressor_registry import get_gain_stats
        stats = get_gain_stats()
    except Exception:
        stats = {
            "pre_tokens": 0,
            "post_tokens": 0,
            "savings_pct": 0.0,
            "compressions": 0,
            "error": "compressor_registry not available",
        }

    return jsonify(stats)
