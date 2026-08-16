import time
from collections import defaultdict

# Simple in-memory rate limiter
# In a production environment, this would use Redis or a similar distributed store.
requests_history = defaultdict(list)

def is_rate_limited(ip_address: str) -> bool:
    now = time.time()
    # Filter out timestamps older than 60 seconds
    requests_history[ip_address] = [t for t in requests_history[ip_address] if now - t < 60]
    
    if len(requests_history[ip_address]) >= 200:
        return True
    
    requests_history[ip_address].append(now)
    return False
