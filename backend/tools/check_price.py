"""Backend implementation for the check_price tool."""

from datetime import datetime, timedelta

def execute(agent, args: dict) -> dict:
    unit_name = args.get("unit_name", "")
    dates = args.get("dates", "")

    try:
        parts = [s.strip() for s in dates.split("->")]
        start = datetime.strptime(parts[0], "%Y-%m-%d")
        end = datetime.strptime(parts[1], "%Y-%m-%d")
    except (ValueError, IndexError):
        return {"error": "Invalid date format. Use yyyy-mm-dd -> yyyy-mm-dd"}

    if start >= end:
        return {"error": "Start date must be before end date"}

    # TODO: replace with actual DB/API lookup
    PRICE_MAP = {
        "Room A": {"weekday": 850000, "weekend": 950000, "cap": "2 dewasa, 2 anak"},
        "Room B": {"weekday": 1200000, "weekend": 1350000, "cap": "4 dewasa, 2 anak"},
        "Room C": {"weekday": 850000, "weekend": 950000, "cap": "2 dewasa, 2 anak"},
        "Room D": {"weekday": 1100000, "weekend": 1250000, "cap": "4 dewasa, 2 anak"},
    }

    rate = PRICE_MAP.get(unit_name)
    if rate is None:
        return {"error": "Unknown room/unit name %s" % unit_name}

    prices = []
    total = 0
    current = start
    while current < end:
        is_weekend = current.weekday() in (5, 6)
        price = rate["weekend"] if is_weekend else rate["weekday"]
        prices.append({
            "date": current.strftime("%Y-%m-%d"),
            "price": price,
            "cap": rate.get("cap", "2 orang")
        })
        total += price
        current += timedelta(days=1)

    return {
        "unit_name": unit_name,
        "dates": dates,
        "currency": "IDR",
        "prices": prices,
        "total": total,
        "nights": len(prices),
    }
