import json
from llm_metering.sweep import sweep_policies, format_2b

res = json.load(open("sweep_2a.json"))
rows = sweep_policies(res, duration=600.0)
print(format_2b(rows))
json.dump(rows, open("sweep_2b.json", "w"), indent=1, default=float)
