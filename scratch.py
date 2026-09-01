import json

d = {"hello": "world\n123"}
print(repr(json.dumps(d)))
