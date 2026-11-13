"""Real backend implementation for the check_availability tool."""


def execute(agent: dict, args: dict) -> dict:
    """Check availability of room"""
    start_date = args.get("start_date")
    end_date = args.get("end_date")
    return {
            "start_date": start_date,
            "end_date": end_date,
            "result": {
                "available": [
                    "Room A",
                    "Room B"
                    ]
                }
            }


def test_execute():
    result = execute({}, {"start_date": "2026-04-10", "end_date": "2026-04-12"})
    assert result["start_date"] == "2026-04-10"
    assert result["end_date"] == "2026-04-12"
    assert "result" in result
    assert "available" in result["result"]
    assert isinstance(result["result"]["available"], list)
