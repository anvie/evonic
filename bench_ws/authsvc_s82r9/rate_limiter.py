from collections import defaultdict
import time

# In-memory storage for rate limiting
# Key: (ip_address, minute_timestamp), Value: count
requests_count = defaultdict(int)

def is_rate_limited(ip_address):
    current_minute = int(time.time() // 60)
    key = (ip_address, current_minute)
    
    count = requests_count[key]
    if count >= 200:
        return True
    
    requests_count[key] += 1
    return False
