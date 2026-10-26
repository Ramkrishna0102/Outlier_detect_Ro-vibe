#!/usr/bin/env python3
"""Creates sample_input.xlsx (ro-vibe style 4 sheets) with PLANTED outliers so you can
see the whole QA flow without real data, plus sample_ro_vibe_result.csv (a dummy
"ro-vibe output") and WATER_BOX.txt (the river area used by mock_osrm_server.py).

    python make_sample_input.py
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(11)
CITY = {"DB001": (25.43, 81.84), "DB002": (26.85, 80.95), "DB003": (28.61, 77.21), "DB004": (19.07, 72.88)}

outlets, truth = [], []


def add(db, oid, lat, lon, beat=None):
    outlets.append({"outlet_id": oid, "outlet_name": f"Shop {oid}", "beat_id": beat or f"B_{db}",
                    "employee_id": f"E_{db}", "lat": lat, "lon": lon, "required_visits": 1})


def cluster(db, n, spread=0.03):
    c = np.array(CITY[db])
    pts = c + rng.normal(0, spread, (n, 2))
    for i, (la, lo) in enumerate(pts):
        add(db, f"{db}_{i:03d}", round(float(la), 6), round(float(lo), 6))


# DB001: 80 normal outlets + 1 lone far outlier + 2 bad-coordinate rows (excluded, never outliers)
cluster("DB001", 80)
add("DB001", "DB001_LONE", 28.2, 81.84); truth.append(("DB001", "DB001_LONE"))
add("DB001", "DB001_ZERO", 0, 0)
add("DB001", "DB001_TEXT", "not-a-number", 81.8)

# DB002: 60 normal + 3 far outliers scattered in different directions (pass-2 case)
cluster("DB002", 60)
for tag, (dla, dlo) in {"N": (2.2, 0.0), "SE": (-1.6, 1.9), "SW": (-1.7, -1.8)}.items():
    add("DB002", f"DB002_FAR_{tag}", CITY["DB002"][0] + dla, CITY["DB002"][1] + dlo)
    truth.append(("DB002", f"DB002_FAR_{tag}"))

# DB003: only 15 outlets + 1 far one -> below min sample, must NOT be flagged
cluster("DB003", 14)
add("DB003", "DB003_FAR", 31.0, 77.21)

# DB004: 70 normal outlets, one of them "in a river" (water box in mock OSRM). Distance-based
# detection cannot see it; only the --check-unroutable test does.
cluster("DB004", 70)
add("DB004", "DB004_RIVER", CITY["DB004"][0] + 0.005, CITY["DB004"][1] + 0.005)

out = pd.DataFrame(outlets)
dbs = sorted(CITY)
with pd.ExcelWriter("sample_input.xlsx") as xw:
    pd.DataFrame({"db_id": dbs, "db_name": [f"Distributor {d}" for d in dbs]}).to_excel(xw, sheet_name="distributors", index=False)
    pd.DataFrame({"beat_id": [f"B_{d}" for d in dbs], "db_id": dbs}).to_excel(xw, sheet_name="beats", index=False)
    pd.DataFrame({"employee_id": [f"E_{d}" for d in dbs]}).to_excel(xw, sheet_name="employees", index=False)
    out.to_excel(xw, sheet_name="outlets", index=False)

# dummy ro-vibe output = only the planted lone/far outliers (a "perfect pass-1+pass-2" ro-vibe)
pd.DataFrame(truth, columns=["db_id", "outlet_id"]).to_csv("sample_ro_vibe_result.csv", index=False)

la, lo = CITY["DB004"][0] + 0.005, CITY["DB004"][1] + 0.005
open("WATER_BOX.txt", "w").write(f"{la-0.002} {la+0.002} {lo-0.002} {lo+0.002}\n")
print("created sample_input.xlsx, sample_ro_vibe_result.csv, WATER_BOX.txt")
