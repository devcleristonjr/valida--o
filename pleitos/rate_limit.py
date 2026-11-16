import hashlib
import hmac
import threading
import time
from collections import deque

from flask import current_app, request


_lock = threading.Lock()


def rate_limit_retry_after(scope, limit, window_seconds):
    client = request.remote_addr or "unknown"
    key = current_app.config["DATA_HASH_KEY"].encode("utf-8")
    client_hash = hmac.new(key, client.encode("utf-8"), hashlib.sha256).hexdigest()
    bucket_key = (scope, client_hash)
    now = time.monotonic()
    state = current_app.extensions.setdefault("pleitos_rate_limits", {})

    with _lock:
        bucket, bucket_window = state.setdefault(bucket_key, (deque(), window_seconds))
        while bucket and now - bucket[0] >= bucket_window:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(1, int(bucket_window - (now - bucket[0])))
        bucket.append(now)
        if len(state) > 4096:
            expired_keys = [
                key for key, (events, stored_window) in state.items()
                if not events or now - events[-1] >= stored_window
            ]
            for key in expired_keys:
                state.pop(key, None)
    return None
