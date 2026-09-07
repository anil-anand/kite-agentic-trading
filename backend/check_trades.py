import json

from backend.kite_client import kite_client

try:
    trades = kite_client.get_trades()
    positions = kite_client.get_positions()

    print("Trades Count:", len(trades))
    if trades:
        print("Sample Trade:", json.dumps(trades[0], indent=2))

    print("Positions Net Count:", len(positions.get("net", [])))
    if positions.get("net"):
        print("Sample Position:", json.dumps(positions.get("net")[0], indent=2))

except Exception as e:
    print("Error:", e)
