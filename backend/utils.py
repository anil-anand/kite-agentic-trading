import json


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        import datetime

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
