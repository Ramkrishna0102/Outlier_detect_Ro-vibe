# outlier_qa - ro-vibe Outlier Detection QA Automation

This tool checks ro-vibe's **geographic outlier detection** from a QA perspective.
Provide it with the same `input.xlsx` that you give ro-vibe. It detects outliers using
its **own, separate implementation**, then compares its results with ro-vibe's results
**by distributor (DB)** and reports PASS / FAIL, including the exact outlet IDs that differ.

---

## 1. Concept (why and how)

**Oracle testing:** The code under test (ro-vibe) is not used to test itself; otherwise,
the same bug could exist in both places and go undetected. This script therefore
implements the outlier logic independently (NumPy + Haversine; it imports nothing from
ro-vibe). If both produce the same answer, the result is PASS; otherwise, it is FAIL.

The script performs three tasks:

| # | Task | When it runs |
|---|---|---|
| 1 | Detect outliers (according to the three ro-vibe code paths) | Always |
| 2 | Compare with ro-vibe results (per DB, per outlet ID) | When `--ro-vibe-result` is provided |
| 3 | Use OSRM to check whether outlets far from a river, sea, or road were flagged | With `--osrm-url` + `--check-unroutable` |

---

## 2. What each file in this folder does

| File | Purpose |
|---|---|
| `outlier_qa.py` | **Main script.** Reads input, detects outliers, compares results, and creates the Excel report. |
| `make_sample_input.py` | Creates demo data with planted outliers: `sample_input.xlsx`, `sample_ro_vibe_result.csv`, and `WATER_BOX.txt`. |
| `mock_osrm_server.py` | **Mock OSRM server** for testing the OSRM code without a real OSRM service. |
| `requirements.txt` | Python libraries: NumPy, pandas, openpyxl, and requests. |
| `README.md` | This file. |

---

## 3. Setup in VS Code (one time)

1. Install Python 3.10 or later (check with `python --version`).
2. In VS Code, open this `outlier_qa` folder using `File > Open Folder`.
3. Open a terminal (`Ctrl + ~`) and run:

```bash
python -m venv .venv

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (cmd):
.venv\Scripts\activate.bat
# Mac / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

4. In VS Code, press `Ctrl+Shift+P` > **Python: Select Interpreter** > select the `.venv` interpreter.

---

## 3A. Two-minute demo (run this first)

```bash
python outlier_qa.py --self-test          # Check the script's math (all 6 tests should PASS)
python make_sample_input.py               # Create demo input
python outlier_qa.py --input sample_input.xlsx --ro-vibe-result sample_ro_vibe_result.csv --compare-mode qualifier
```

This creates `outlier_qa_report.xlsx`. The demo shows:
- `DB001`: One isolated, distant outlet is detected.
- `DB002`: Three distant outlets in different directions (two pass using one, and the third passes using two).
- `DB003`: Only 15 outlets, so no outliers are reported (minimum is 20).
- `DB004`: One outlet is "in a river" (only the OSRM test detects it).
- Two rows in `DB001` with invalid coordinates (`0,0` and text) are **excluded**.

Run the same demo with `--compare-mode phase1` and `DB002` will FAIL because Phase 1 does not have Pass 2.
This is not a bug; the modes differ (see section 6).

### OSRM demo (with a mock server)

Terminal 1:
```bash
python mock_osrm_server.py --port 5000 --water 19.073 19.077 72.883 72.887
```
Terminal 2:
```bash
python outlier_qa.py --input sample_input.xlsx --ro-vibe-result sample_ro_vibe_result.csv --compare-mode phase1 --osrm-url http://127.0.0.1:5000/india --check-unroutable
```
The `Unroutable_check` sheet will show `DB004_RIVER` as **FAIL**, meaning ro-vibe did not flag it.
This is the gap described in section 7.

---

## 4. How to run with real data

```bash
python outlier_qa.py --input "C:\path\input.xlsx" --ro-vibe-result "C:\path\ro_vibe_outliers.csv" --compare-mode phase1
```

### Input file (the same one you give ro-vibe)
- **Four-sheet workbook:** `distributors`, `beats`, `employees`, and `outlets`.
  The `outlets` sheet needs `outlet_id, beat_id, lat, lon`; `db_id` is obtained from the `beats` sheet through `beat_id`.
- **Or a flat sheet:** containing `outlet_id`, `lat`, `lon`, and `db_id` (or `distributor_id`) columns.
- Spaces, underscores, and capitalization in headers do not matter (`Outlet ID` = `outlet_id`).
  Recognized names: `db_id, distributor_id, outlet_id, outlet_erp_id, lat, latitude, lon, lng, longitude, beat_id`.

### ro-vibe result file (created from ro-vibe's output)
Provide a CSV or Excel file with **one row per outlier**:

| db_id | outlet_id |
|---|---|
| DB001 | DB001_LONE |
| DB002 | DB002_FAR_N |

If your file has one row per outlet and an `is_outlier` column, pass
`--rv-flag-col is_outlier` (only rows with true / 1 / yes are counted).
If the column names differ, pass `--rv-db-col` and `--rv-outlet-col`. JSON is also supported:
`{"DB001": ["id1","id2"]}`.

For a DB with no outlets in the ro-vibe result file, the script assumes ro-vibe reported
**zero outliers**.

---

## 5. Logic - step by step

### Step 0 - Cleaning (exclusions)
As in ro-vibe, the following are excluded **before** detection:
- Empty `db_id` or `outlet_id`
- Non-numeric latitude / longitude
- `0,0` coordinates
- Latitude / longitude outside the bounds (default +-90 / +-180)

These outlets are listed in the `Excluded_outlets` sheet and are not included in the outlier count.

### Step 1 - Group by DB
Outlets are grouped by `db_id`; the mean, standard deviation, and threshold are calculated
separately **within each DB** (not across the entire dataset).

### Step 2 - Pairwise distance matrix (N x N)
Calculate the distance in kilometers from every outlet to every other outlet.
- **Haversine:** Great-circle distance multiplied by `road_factor` (default **1.5**) to approximate road distance.
- **OSRM:** Actual road distance (the `/table` API). If OSRM cannot provide a cell, fall back to Haversine x 1.5.
  The road factor is **never** applied to OSRM distances, which are already road distances.

### Step 3 - Pass 1: global threshold + majority rule
```
mean      = average of all pairwise distances (excluding the diagonal zeros)
std       = standard deviation of those distances (excluding the diagonal)
threshold = mean + sigma * std            (sigma defaults to 3.0)

outlet X is an outlier if:
(number of OTHER outlets farther than the threshold from X) > 50% of (N-1)
```
This does **not** use a centroid. It is a "population-wide" signal: a single distant outlet
cannot pull the centroid toward itself and hide, and two distant outlets close to each other
can still both be detected.

If a DB has fewer than 20 outlets, no outliers are reported (`--min-sample`).

### Step 4 - Pass 2 (only in `qualifier` mode): centroid + k-NN
An outlet that remains after Pass 1 is also considered an outlier if **both** conditions hold:
1. **Distance from centroid > 50 km** (centroid = simple average of all outlet latitudes and longitudes)
2. **Median distance to its 5 nearest outlets > 100 km**

This catches cases where several distant outlets are spread in different directions and each
outlet's "far count" is diluted (none individually crosses the 50% threshold).

### Step 5 - Compare
For each DB:
```
script_outliers      = outlier IDs detected by this script
ro_vibe_outliers     = outlier IDs detected by ro-vibe
MISSED_BY_RO_VIBE    = detected by this script, not by ro-vibe
EXTRA_IN_RO_VIBE     = detected by ro-vibe, not by this script
PASS = both sets are empty; FAIL = either set is non-empty
```
Each mismatch includes `far_fraction`, `mean_dist_km`, `centroid_km`, `knn_median_km`, and
a **BORDERLINE** note (when it is within 2% of the 50% rule), to help determine whether the
difference is a real bug or just a floating-point / tie boundary case.

---

## 6. Three modes (`--compare-mode`)

ro-vibe detects outliers differently in three places. Choose the mode that matches the result
you are comparing:

| Mode | ro-vibe code | Logic | Distance |
|---|---|---|---|
| `excel` | `excel_import._detect_geo` | Pass 1 only, for each `db_id`. Advisory warning only (`GEO_OUTLIER`). | Haversine x 1.5 |
| `qualifier` | `qualifier._geo_outlier_ids` | Pass 1 + Pass 2. Result is RED `FAR_GEOGRAPHIC_OUTLIER`. | Haversine x 1.5 |
| `phase1` | `rep_zoning.compute_rep_zones` | Pass 1 only; outliers are removed from zoning. | **Road distance when OSRM is enabled** |

The report includes results for all three modes (`Summary_per_DB`, `Outlier_details`).
Comparison is performed only for the mode selected by `--compare-mode` (default `phase1`).

---

## 7. OSRM and the river / sea test

```bash
export OSRM_AUTH_HEADER="Basic xxxxxxxx"      # If the host requires authentication (Windows: set / $env:)
python outlier_qa.py --input input.xlsx --ro-vibe-result rv.csv --compare-mode phase1 \
    --osrm-url https://<osrm-host>/<country> --insecure --check-unroutable
```
- `--osrm-url` = the full host URL, including the country path (the script appends `/table/...` to this URL).
- Use `--insecure` only if the host has a self-signed certificate.
- Never put credentials in a file; provide them only through the `OSRM_AUTH_HEADER` environment variable.

**Unroutable test logic:** For each outlet, the script queries the OSRM `/nearest` API to find
the distance to the nearest road. If the snap distance is greater than `--snap-threshold-m`
(default 500 m), or OSRM cannot snap the outlet (for example, it is in a river / sea or outside
the road network), the outlet is considered **UNROUTABLE**. The script then checks whether
ro-vibe flagged it: flagged = PASS, not flagged = FAIL.

**Note:** ro-vibe's outlier logic checks **distance** only. If an outlet in a river is still
within the cluster, OSRM may snap it to a nearby road and it will not be an outlier. Therefore,
this test can FAIL, and that is a **valid finding** (a gap between the requirement and actual
behavior), not a bug in the script.

---

## 8. Report (`outlier_qa_report.xlsx`) sheets

| Sheet | Meaning |
|---|---|
| `Comparison_per_DB` | For each DB: script vs. ro-vibe outlier counts, matched / missed / extra, and **PASS / FAIL** |
| `Mismatched_outlets` | Only outlets that differ, with their metrics and a BORDERLINE note |
| `Unroutable_check` | River / sea / no-road outlets and whether ro-vibe flagged them |
| `Summary_per_DB` | Each DB's threshold, mean, std, and outlier counts and IDs for all three modes |
| `Outlier_details` | Metrics for **every valid outlet** (`far_count`, `far_fraction`, `centroid_km`, `knn_median_km`, and flags) |
| `Excluded_outlets` | Outlets excluded because of invalid coordinates / IDs, with the reason |
| `Run_config` | All parameters used for the run, so the result can be reproduced |

Exit code: `0` if all checks PASS; `1` if any check FAILs (useful for CI / batch runs).

---

## 9. Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `--sigma` | 3.0 | Threshold = mean + sigma x std |
| `--min-sample` | 20 | No outliers are reported for DBs with fewer outlets than this |
| `--excel-min-outlets` | 20 | Separate minimum for excel mode |
| `--road-factor` | 1.5 | Haversine road-distance multiplier |
| `--knn-k` / `--knn-km` | 5 / 100 | Pass 2: number of nearest outlets / median distance limit |
| `--centroid-far-km` | 50 | Pass 2: distance-from-centroid limit |
| `--centroid-road-factor` | 1.0 | Factor for Pass 2 centroid distance (not verified) |
| `--lat-min/max`, `--lon-min/max` | +-90 / +-180 | Valid coordinate range |
| `--osrm-chunk / -timeout / -workers` | 100 / 15 s / 4 | OSRM request settings |
| `--snap-threshold-m` | 500 | Distance from road above which an outlet is considered unroutable |

---

## 10. What has NOT been verified (please read)

This script was built by reading ro-vibe's code. The actual values / behavior of the
following were unavailable, so these are **assumptions**. If a mismatch occurs, check these first:

1. **Excel mode `sigma` and minimum outlet count** (`_OUTLIER_SIGMA`, `_GEO_MIN_OUTLETS` in `excel_import.py`): assumed to be 3.0 and 20.
2. **Phase 1 `cfg.outlier_sigma`** (`RepZoningConfig`): assumed to default to 3.0.
3. Whether **Pass 2 centroid distance** is multiplied by the road factor (`_haversine_km` in `qualifier.py`): assumed **not** (1.0). If a mismatch occurs near 50 km, try `--centroid-road-factor 1.5`.
4. **`MO_FAR_FROM_DB_KM` = 50 km** was taken from a code comment; the variable's value was not checked.
5. **Latitude / longitude bounds** (`LAT_MIN`, etc.): ro-vibe's constants were not available, so +-90 / +-180 are used.
6. The exact **OSRM URL path pattern** and ro-vibe's fallback when OSRM fails (`osrm_provider.py`) were not checked; based on the docstring, this script assumes "use Haversine where OSRM has no result."
7. **Phase 1 grouping:** assumed that outlets from one DB are processed together during detection.
8. The exact flat-file column names were not checked, so the script recognizes aliases.
9. The **ro-vibe output format** was unknown, so a simple two-column result format is used.
10. The script has **not been tested against a real OSRM host**. OSRM code was tested only with `mock_osrm_server.py`.

---

## 11. Common problems

| Problem | Solution |
|---|---|
| `Could not find an outlets sheet` | Name the sheet `outlets`, or provide a flat sheet with `outlet_id, lat, lon, db_id` columns. |
| `ro-vibe result needs db + outlet columns` | Provide the column names with `--rv-db-col` and `--rv-outlet-col`. |
| All DBs are FAILing | The `--compare-mode` may be wrong (section 6), or the IDs in the ro-vibe result file may use a different format (`101` vs. `101.0`; the script normalizes these automatically). |
| Only some outlets have BORDERLINE mismatches | They are right on the edge of the 50% rule; small floating-point / OSRM differences can change the result. It is not necessarily a bug, but note it. |
| `osrm_fallback_cells_pct` is high | OSRM requests are failing (URL, authentication, timeout, or TLS). Check `--insecure` / `OSRM_AUTH_HEADER`. The Phase 1 result will then be mostly like Haversine. |
| Memory use / slow performance | OSRM is not used for DBs larger than 10,000 outlets (as in ro-vibe); the matrix is N x N, so very large DBs take longer. |

---

## 12. Limitations

- This script checks **geographic outliers** only, not ro-vibe's other rules (TSP, zoning, or qualifier visit-based rules).
- There is no ground truth for "which outlets are truly outliers." This tool checks whether **ro-vibe's logic matches its specification**; it does not establish absolute truth. Depending on the business context, some geographically distant outlets may be valid.
