import datetime
import json
import sys
import threading
import uuid

from .time_utils import now_utc

stdout_lock = threading.Lock()


def push_log(message: str, level: str = "info"):
    event = {
        "event": "log:entry",
        "data": {
            "id": str(uuid.uuid4()),
            "level": level,
            "message": message,
            "timestamp": now_utc().isoformat(),
        },
    }
    with stdout_lock:
        print(json.dumps(event, cls=DateTimeEncoder))
        sys.stdout.flush()


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        try:
            import numpy as np

            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)
