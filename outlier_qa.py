#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
outlier_qa.py - Independent QA "oracle" for ro-vibe geographic outlier detection.

WHAT IT DOES
    1. Reads the SAME input.xlsx you give to ro-vibe.
    2. Re-computes geographic outliers per distributor (DB) with its OWN code
       (it does not import anything from ro-vibe).
    3. Optionally compares its result with the outlier list ro-vibe produced
       and reports PASS / FAIL per DB, plus the exact outlet ids that differ.
    4. Optionally (OSRM): checks that outlets that are NOT reachable by road
       (river / sea / no road nearby) are flagged as outliers by ro-vibe.

It mirrors the three places ro-vibe detects outliers:
    excel      excel_import._detect_geo      -> pass 1, haversine x road factor
    qualifier  qualifier._geo_outlier_ids    -> pass 1 + pass 2 (centroid + k-NN)
    phase1     rep_zoning.compute_rep_zones  -> pass 1, OSRM road distance if on

Run  `python outlier_qa.py --help`  or read README.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants (defaults copied from what was read in the ro-vibe code)
# --------------------------------------------------------------------------- #
EARTH_R_KM = 6371.0088      # same Earth radius ro-vibe uses
ROAD_FACTOR = 1.5           # great-circle -> road circuity (haversine only)
BLOCK = 512                 # row-block size, keeps memory O(block * n)
OSRM_MAX_N = 10_000         # ro-vibe: above this OSRM is not used for outliers

DEFAULT_SIGMA = 3.0
DEFAULT_MIN_SAMPLE = 20
KNN_K = 5
KNN_KM = 100.0
CENTROID_FAR_KM = 50.0

MODES = ("excel", "qualifier", "phase1")
MODE_COL = {"excel": "excel_flag", "qualifier": "qualifier_flag", "phase1": "phase1_flag"}

# --------------------------------------------------------------------------- #
# Column-name helpers (headers are matched case/space/underscore-insensitively)
# --------------------------------------------------------------------------- #
ALIASES = {
    "db": {"dbid", "distributorid", "distributorcode", "dbcode", "distid"},
    "outlet": {"outletid", "outleterpid", "outletcode", "locationid", "storeid"},
    "name": {"outletname", "name", "storename"},
    "lat": {"lat", "latitude", "outletlat", "outletlatitude"},
    "lon": {"lon", "lng", "long", "longitude", "outletlon", "outletlongitude"},
    "beat": {"beatid", "beat", "beatcode"},
}


def nk(s) -> str:
    """normalise a header / sheet name: 'Outlet_ID ' -> 'outletid'."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def find_col(df: pd.DataFrame, kind: str, explicit: str | None = None):
    if explicit:
        for c in df.columns:
            if str(c) == explicit or nk(c) == nk(explicit):
                return c
        raise SystemExit(f"Column '{explicit}' not found. Columns: {list(df.columns)}")
    for c in df.columns:
        if nk(c) in ALIASES[kind]:
            return c
    return None


def to_id(v) -> str:
    """Excel gives 101.0 for 101 -> normalise every id to a clean string."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return str(int(v)) if float(v).is_integer() else str(v)
    return str(v).strip()


# --------------------------------------------------------------------------- #
# 1. Load + clean the input workbook
# --------------------------------------------------------------------------- #
def load_input(path: str) -> pd.DataFrame:
    """Return DataFrame[db_id, outlet_id, outlet_name, lat_raw, lon_raw, src_row].

    Supports ro-vibe's 4-sheet workbook (distributors/beats/employees/outlets,
    db_id reached through beats) and a flat sheet that already has a db column.
    """
    if path.lower().endswith(".csv"):
        sheets = {"outlets": pd.read_csv(path, dtype=object)}
    else:
        sheets = pd.read_excel(path, sheet_name=None, dtype=object)
    by = {nk(k): v for k, v in sheets.items()}

    cand = by.get("outlets")
    if cand is None:
        for df in sheets.values():
            if find_col(df, "outlet") and find_col(df, "lat") and find_col(df, "lon"):
                cand = df
                break
    if cand is None:
        raise SystemExit("Could not find an outlets sheet (needs outlet id + lat + lon columns).")

    c_out, c_lat, c_lon = find_col(cand, "outlet"), find_col(cand, "lat"), find_col(cand, "lon")
    if not (c_out and c_lat and c_lon):
        raise SystemExit(f"Outlets sheet needs outlet id / lat / lon columns. Found: {list(cand.columns)}")
    c_db, c_name, c_beat = find_col(cand, "db"), find_col(cand, "name"), find_col(cand, "beat")

    if c_db:
        db = cand[c_db].map(to_id)
    elif c_beat:
        beats = by.get("beats")
        if beats is None:
            raise SystemExit("Outlets have beat ids but there is no 'beats' sheet to map beat -> db_id.")
        b_beat, b_db = find_col(beats, "beat"), find_col(beats, "db")
        if not (b_beat and b_db):
            raise SystemExit("'beats' sheet needs beat_id and db_id columns.")
        m = {to_id(a): to_id(b) for a, b in zip(beats[b_beat], beats[b_db])}
        db = cand[c_beat].map(lambda x: m.get(to_id(x), ""))
    else:
        raise SystemExit("Outlets sheet has neither a db/distributor column nor a beat column.")

    out = pd.DataFrame(
        {
            "db_id": db.values,
            "outlet_id": cand[c_out].map(to_id).values,
            "outlet_name": (cand[c_name].map(to_id).values if c_name else ""),
            "lat_raw": cand[c_lat].values,
            "lon_raw": cand[c_lon].values,
            "src_row": np.arange(len(cand)) + 2,  # Excel row number (header = row 1)
        }
    )
    return out


def split_valid(df: pd.DataFrame, lat_min, lat_max, lon_min, lon_max):
    """Apply the same exclusions ro-vibe applies BEFORE outlier detection."""
    lat = pd.to_numeric(df["lat_raw"], errors="coerce")
    lon = pd.to_numeric(df["lon_raw"], errors="coerce")
    reason = np.select(
        [
            df["db_id"].eq("") | df["outlet_id"].eq(""),
            lat.isna() | lon.isna(),
            (lat == 0) & (lon == 0),
            (lat < lat_min) | (lat > lat_max) | (lon < lon_min) | (lon > lon_max),
        ],
        ["MISSING_DB_OR_OUTLET_ID", "NON_NUMERIC_COORDS", "ZERO_ZERO_COORDS", "OUT_OF_BOUNDS"],
        default="",
    )
    df = df.assign(lat=lat, lon=lon, exclude_reason=reason)
    bad = df[df["exclude_reason"] != ""].drop(columns=["lat", "lon"])
    good = df[df["exclude_reason"] == ""].drop(columns=["lat_raw", "lon_raw", "exclude_reason"])
    dup = good["outlet_id"].duplicated(keep=False)
    if dup.any():
        print(f"[warn] {int(dup.sum())} rows share a duplicate outlet_id (ro-vibe rejects these). "
              f"First occurrence kept.")
        good = good[~good["outlet_id"].duplicated(keep="first")]
    return good.reset_index(drop=True), bad.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 2. Distance maths
# --------------------------------------------------------------------------- #
def hav_km(lat1, lon1, lat2, lon2):
    """Great-circle km, inputs in RADIANS (atan2 form of haversine)."""
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    a = np.clip(a, 0.0, 1.0)
    return 2.0 * EARTH_R_KM * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


def make_hav_block_fn(coords: np.ndarray, factor: float):
    lat, lon = np.radians(coords[:, 0]), np.radians(coords[:, 1])

    def fn(s: int, e: int) -> np.ndarray:  # rows [s:e) of the NxN km matrix
        return hav_km(lat[s:e, None], lon[s:e, None], lat[None, :], lon[None, :]) * factor

    return fn


# --------------------------------------------------------------------------- #
# 3. Pass 1: global mean + sigma*std threshold, "more than half are farther"
# --------------------------------------------------------------------------- #
@dataclass
class P1:
    ran: bool
    n: int
    mean: float
    std: float
    thr: float
    idx: np.ndarray        # indices flagged
    far: np.ndarray        # per point: how many OTHER points are farther than thr
    far_frac: np.ndarray   # far / (n-1)
    mean_dist: np.ndarray  # per point: mean km to all other points


def pass1(n: int, block_fn, sigma: float, min_sample: int) -> P1:
    if n < min_sample:
        z = np.zeros(n)
        return P1(False, n, math.nan, math.nan, math.nan, np.array([], dtype=int),
                  np.zeros(n, dtype=int), z, z.copy())
    total = total_sq = 0.0
    for s in range(0, n, BLOCK):
        b = block_fn(s, min(s + BLOCK, n)).astype(np.float64)
        total += float(b.sum())
        total_sq += float(np.square(b).sum())
    cnt = n * (n - 1)                      # OFF-diagonal count (diagonal zeros add 0)
    mean = total / cnt
    std = math.sqrt(max(total_sq / cnt - mean * mean, 0.0))
    thr = mean + sigma * std
    far = np.zeros(n, dtype=int)
    rowsum = np.zeros(n)
    for s in range(0, n, BLOCK):
        e = min(s + BLOCK, n)
        b = block_fn(s, e).astype(np.float64)
        far[s:e] = (b > thr).sum(axis=1)
        rowsum[s:e] = b.sum(axis=1)
    frac = far / (n - 1)
    idx = np.where(far > 0.5 * (n - 1))[0]
    return P1(True, n, mean, std, thr, idx, far, frac, rowsum / (n - 1))


def knn_median_km(coords: np.ndarray, k: int, factor: float) -> np.ndarray:
    """Median km to the k nearest OTHER points (local isolation signal)."""
    n = len(coords)
    k = min(k, n - 1)
    if k <= 0:
        return np.zeros(n)
    fn = make_hav_block_fn(coords, factor)
    out = np.empty(n)
    for s in range(0, n, BLOCK):
        e = min(s + BLOCK, n)
        b = fn(s, e)
        b[np.arange(e - s), np.arange(s, e)] = np.inf       # exclude self
        out[s:e] = np.median(np.partition(b, k - 1, axis=1)[:, :k], axis=1)
    return out


# --------------------------------------------------------------------------- #
# 4. OSRM helpers (only used when --osrm-url is given)
# --------------------------------------------------------------------------- #
def osrm_headers() -> dict:
    h = {}
    if os.getenv("OSRM_AUTH_HEADER"):               # e.g.  export OSRM_AUTH_HEADER="Basic xxxx"
        h["Authorization"] = os.environ["OSRM_AUTH_HEADER"]
    return h


def osrm_matrix_km(coords, base_url, chunk, timeout, workers, verify, factor):
    """NxN road-km matrix from OSRM /table. Cells OSRM cannot answer fall back to
    haversine x factor (same as ro-vibe). Returns (matrix_km, stats)."""
    import requests

    n = len(coords)
    D = np.zeros((n, n), dtype=np.float32)
    hdr = osrm_headers()
    lat, lon = np.radians(coords[:, 0]), np.radians(coords[:, 1])
    stats = {"cells": n * (n - 1), "fallback_cells": 0, "failed_chunks": 0}

    def task(i, j):
        src, dst = coords[i:i + chunk], coords[j:j + chunk]
        pts = np.vstack([src, dst])
        cs = ";".join(f"{lo:.6f},{la:.6f}" for la, lo in pts)
        srcs = ";".join(str(x) for x in range(len(src)))
        dsts = ";".join(str(x) for x in range(len(src), len(pts)))
        url = f"{base_url}/table/v1/driving/{cs}?sources={srcs}&destinations={dsts}&annotations=distance"
        fb = hav_km(lat[i:i + chunk, None], lon[i:i + chunk, None],
                    lat[None, j:j + chunk], lon[None, j:j + chunk]) * factor
        try:
            r = requests.get(url, headers=hdr, timeout=timeout, verify=verify)
            r.raise_for_status()
            rows = r.json().get("distances")
            arr = np.array([[np.nan if v is None else v for v in row] for row in rows], dtype=float) / 1000.0
            if arr.shape != fb.shape:
                raise ValueError("unexpected OSRM table shape")
            bad = np.isnan(arr) | (arr < 0)
            arr[bad] = fb[bad]
            return i, j, arr, int(bad.sum()), 0
        except Exception:
            return i, j, fb, fb.size, 1

    jobs = [(i, j) for i in range(0, n, chunk) for j in range(0, n, chunk)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, j, arr, nfb, failed in ex.map(lambda a: task(*a), jobs):
            D[i:i + arr.shape[0], j:j + arr.shape[1]] = arr
            stats["fallback_cells"] += nfb
            stats["failed_chunks"] += failed
    np.fill_diagonal(D, 0.0)
    return D, stats


def osrm_snap_m(coords, base_url, timeout, workers, verify) -> np.ndarray:
    """Distance (m) from each point to the nearest road. NaN = OSRM cannot snap it."""
    import requests

    hdr = osrm_headers()

    def one(p):
        try:
            r = requests.get(f"{base_url}/nearest/v1/driving/{p[1]:.6f},{p[0]:.6f}?number=1",
                             headers=hdr, timeout=timeout, verify=verify)
            if r.status_code != 200:
                return math.nan
            return float(r.json()["waypoints"][0]["distance"])
        except Exception:
            return math.nan

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return np.array(list(ex.map(one, coords)), dtype=float)


# --------------------------------------------------------------------------- #
# 5. Run all modes per DB
# --------------------------------------------------------------------------- #
def analyse(good: pd.DataFrame, a) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list]:
    details, summary, unroutable, notes = [], [], [], []
    verify = not a.insecure

    for db, g in good.groupby("db_id", sort=True):
        g = g.reset_index(drop=True)
        n = len(g)
        coords = g[["lat", "lon"]].to_numpy(dtype=float)
        rec = g[["db_id", "outlet_id", "outlet_name"]].copy()
        rec["lat"], rec["lon"] = coords[:, 0], coords[:, 1]

        # ---- haversine pass 1 (used by excel + qualifier, and phase1 when OSRM is off)
        h_fn = make_hav_block_fn(coords, a.road_factor)
        p1h = pass1(n, h_fn, a.sigma, a.min_sample)
        rec["far_count_hav"], rec["far_fraction_hav"] = p1h.far, p1h.far_frac
        rec["mean_dist_km_hav"] = p1h.mean_dist
        is_p1h = np.zeros(n, dtype=bool)
        is_p1h[p1h.idx] = True

        # ---- excel mode (advisory warning in excel_import)
        excel_ok = n >= max(a.excel_min_outlets, a.min_sample)
        rec["excel_flag"] = is_p1h if excel_ok else False

        # ---- qualifier mode: pass 1 + pass 2
        rec["qualifier_pass1"] = is_p1h
        centroid = coords.mean(axis=0)
        clat, clon = math.radians(centroid[0]), math.radians(centroid[1])
        d_cent = hav_km(np.radians(coords[:, 0]), np.radians(coords[:, 1]), clat, clon) * a.centroid_road_factor
        knn = knn_median_km(coords, a.knn_k, a.road_factor) if n >= a.min_sample else np.zeros(n)
        rec["centroid_km"], rec["knn_median_km"] = d_cent, knn
        pass2 = (~is_p1h) & (d_cent > a.centroid_far_km) & (knn > a.knn_km) & (n >= a.min_sample)
        rec["qualifier_pass2"] = pass2
        rec["qualifier_flag"] = is_p1h | pass2

        # ---- phase1 mode: OSRM road distance when enabled, else haversine
        osrm_used, osrm_stats, p1p = False, None, p1h
        if a.osrm_url and a.min_sample <= n <= OSRM_MAX_N:
            D, osrm_stats = osrm_matrix_km(coords, a.osrm_url, a.osrm_chunk, a.osrm_timeout,
                                           a.osrm_workers, verify, a.road_factor)
            p1p = pass1(n, lambda s, e, D=D: D[s:e], a.sigma, a.min_sample)
            osrm_used = True
            del D
        elif a.osrm_url and n > OSRM_MAX_N:
            notes.append(f"DB {db}: n={n} > {OSRM_MAX_N}; ro-vibe falls back to haversine, so does this script.")
        is_p1p = np.zeros(n, dtype=bool)
        is_p1p[p1p.idx] = True
        rec["phase1_flag"] = is_p1p
        rec["phase1_far_fraction"] = p1p.far_frac

        # ---- OSRM unroutable check (river / sea / no road nearby)
        if a.osrm_url and a.check_unroutable:
            snap = osrm_snap_m(coords, a.osrm_url, a.osrm_timeout, a.osrm_workers, verify)
            rec["snap_m"] = snap
            rec["unroutable"] = np.isnan(snap) | (snap > a.snap_threshold_m)

        details.append(rec)
        summary.append({
            "db_id": db, "valid_outlets": n,
            "below_min_sample": n < a.min_sample,
            "hav_threshold_km": p1h.thr, "hav_mean_km": p1h.mean, "hav_std_km": p1h.std,
            "excel_outliers": int(rec["excel_flag"].sum()),
            "qualifier_pass1": int(is_p1h.sum()), "qualifier_pass2": int(pass2.sum()),
            "qualifier_outliers": int(rec["qualifier_flag"].sum()),
            "phase1_distance": "OSRM" if osrm_used else "haversine",
            "phase1_threshold_km": p1p.thr, "phase1_outliers": int(is_p1p.sum()),
            "osrm_fallback_cells_pct": (round(100.0 * osrm_stats["fallback_cells"] / max(osrm_stats["cells"], 1), 2)
                                        if osrm_stats else None),
            "excel_ids": ",".join(rec.loc[rec["excel_flag"], "outlet_id"]),
            "qualifier_ids": ",".join(rec.loc[rec["qualifier_flag"], "outlet_id"]),
            "phase1_ids": ",".join(rec.loc[rec["phase1_flag"], "outlet_id"]),
        })

    det = pd.concat(details, ignore_index=True) if details else pd.DataFrame()
    return det, pd.DataFrame(summary), notes


# --------------------------------------------------------------------------- #
# 6. Compare with the ro-vibe result
# --------------------------------------------------------------------------- #
TRUE = {"1", "true", "yes", "y", "t"}


def load_rv_result(path, db_col, outlet_col, flag_col) -> dict[str, set]:
    """ro-vibe outliers as {db_id: {outlet_id,...}}. File = csv/xlsx with one row
    per outlier (db + outlet columns), or json ({db:[ids]} or [{db_id,outlet_id}])."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {to_id(k): {to_id(x) for x in v} for k, v in data.items()}
        df = pd.DataFrame(data)
    elif ext in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, dtype=object)
    else:
        df = pd.read_csv(path, dtype=object)
    c_db, c_out = find_col(df, "db", db_col), find_col(df, "outlet", outlet_col)
    if not (c_db and c_out):
        raise SystemExit(f"ro-vibe result needs db + outlet columns (use --rv-db-col/--rv-outlet-col). "
                         f"Found: {list(df.columns)}")
    if flag_col:
        fc = find_col(df, "db", flag_col)
        keep = df[fc].map(lambda v: to_id(v).lower() in TRUE or (to_id(v).replace('.', '', 1).isdigit()
                                                                  and float(to_id(v)) != 0))
        df = df[keep]
    res: dict[str, set] = {}
    for d, o in zip(df[c_db].map(to_id), df[c_out].map(to_id)):
        if d and o:
            res.setdefault(d, set()).add(o)
    return res


def compare(det: pd.DataFrame, rv: dict[str, set], mode: str, a):
    col = MODE_COL[mode]
    mine = {k: set(g.loc[g[col], "outlet_id"]) for k, g in det.groupby("db_id")}
    all_dbs = sorted(set(det["db_id"]) | set(rv))
    frac_col = "phase1_far_fraction" if mode == "phase1" else "far_fraction_hav"
    lookup = det.set_index(["db_id", "outlet_id"])
    rows, mism = [], []
    for db in all_dbs:
        s, r = mine.get(db, set()), rv.get(db, set())
        missed, extra = sorted(s - r), sorted(r - s)
        n_valid = int((det["db_id"] == db).sum())
        rows.append({"db_id": db, "valid_outlets": n_valid, "script_outliers": len(s),
                     "ro_vibe_outliers": len(r), "matched": len(s & r),
                     "missed_by_ro_vibe": len(missed), "extra_in_ro_vibe": len(extra),
                     "result": "PASS" if not missed and not extra else "FAIL",
                     "script_ids": ",".join(sorted(s)), "ro_vibe_ids": ",".join(sorted(r))})
        for kind, ids in (("MISSED_BY_RO_VIBE", missed), ("EXTRA_IN_RO_VIBE", extra)):
            for oid in ids:
                m = {"db_id": db, "outlet_id": oid, "issue": kind}
                if (db, oid) in lookup.index:
                    x = lookup.loc[(db, oid)]
                    m.update({"far_fraction": round(float(x[frac_col]), 4),
                              "mean_dist_km": round(float(x["mean_dist_km_hav"]), 2),
                              "centroid_km": round(float(x["centroid_km"]), 2),
                              "knn_median_km": round(float(x["knn_median_km"]), 2)})
                    near_half = abs(float(x[frac_col]) - 0.5) <= 0.02
                    near_p2 = mode == "qualifier" and (abs(x["centroid_km"] / a.centroid_far_km - 1) < 0.05
                                                       or abs(x["knn_median_km"] / a.knn_km - 1) < 0.05)
                    m["note"] = ("BORDERLINE: within 2% of the 50% rule" if near_half
                                 else "BORDERLINE: near pass-2 limit" if near_p2 else "")
                else:
                    m["note"] = "outlet not in script's valid set (excluded / unknown id / DB below sample?)"
                mism.append(m)
    return pd.DataFrame(rows), pd.DataFrame(mism)


# --------------------------------------------------------------------------- #
# 7. Report
# --------------------------------------------------------------------------- #
def write_report(path, sheets: dict[str, pd.DataFrame]):
    from openpyxl.styles import Font, PatternFill
    green = PatternFill("solid", fgColor="C6EFCE")
    red = PatternFill("solid", fgColor="FFC7CE")
    head = PatternFill("solid", fgColor="D9E1F2")
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for c in ws[1]:
                c.font, c.fill = Font(bold=True), head
            for i, col in enumerate(df.columns, start=1):
                lens = [len(str(v)) for v in df[col].head(2000)] or [0]
                width = min(60, max(10, int(np.percentile(lens, 90)) + 2, len(str(col)) + 2))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
            for ci, col in enumerate(df.columns, start=1):
                if col in ("result", "check"):
                    for r in range(2, len(df) + 2):
                        cell = ws.cell(row=r, column=ci)
                        cell.fill = green if cell.value == "PASS" else red if cell.value == "FAIL" else cell.fill


# --------------------------------------------------------------------------- #
# 8. Self-test (proves the script's own maths before you trust it)
# --------------------------------------------------------------------------- #
def _naive_pass1(coords, sigma, factor):
    """Deliberately dumb O(n^2) python loops - cross-check for the numpy version."""
    n = len(coords)
    r = lambda d: math.radians(d)
    D = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                la1, lo1, la2, lo2 = r(coords[i][0]), r(coords[i][1]), r(coords[j][0]), r(coords[j][1])
                a = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
                D[i][j] = 2 * EARTH_R_KM * math.asin(math.sqrt(a)) * factor
    vals = [D[i][j] for i in range(n) for j in range(n) if i != j]
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    thr = mean + sigma * std
    return {i for i in range(n) if sum(1 for j in range(n) if j != i and D[i][j] > thr) > 0.5 * (n - 1)}


def self_test() -> int:
    rng = np.random.default_rng(7)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok &= bool(cond)

    print("Self-test")
    base = np.array([25.43, 81.84])
    # 1) numpy vs naive loops on random data
    pts = np.vstack([base + rng.normal(0, 0.04, (45, 2)), base + [2.5, 2.0], base + [-2.2, 1.9]])
    got = set(pass1(len(pts), make_hav_block_fn(pts, ROAD_FACTOR), 3.0, 20).idx.tolist())
    check("numpy pass-1 == naive double-loop pass-1", got == _naive_pass1(pts.tolist(), 3.0, ROAD_FACTOR))
    # 2) lone far outlier is flagged
    lone = np.vstack([base + rng.normal(0, 0.03, (59, 2)), base + [2.8, 0.0]])
    p = pass1(60, make_hav_block_fn(lone, ROAD_FACTOR), 3.0, 20)
    check("lone far outlier (index 59) flagged by pass 1", p.idx.tolist() == [59])
    # 3) tight cluster, no outliers
    tight = base + rng.normal(0, 0.03, (60, 2))
    check("tight cluster -> no outliers", len(pass1(60, make_hav_block_fn(tight, ROAD_FACTOR), 3.0, 20).idx) == 0)
    # 4) below min sample -> nothing
    small = np.vstack([base + rng.normal(0, 0.03, (14, 2)), base + [3, 3]])
    check("n=15 (< 20) -> pass 1 skipped", not pass1(15, make_hav_block_fn(small, ROAD_FACTOR), 3.0, 20).ran)
    # 5) 2 far points side by side cannot hide behind each other
    pair = np.vstack([base + rng.normal(0, 0.03, (58, 2)), base + [2.8, 0.0], base + [2.8, 0.01]])
    p = pass1(60, make_hav_block_fn(pair, ROAD_FACTOR), 3.0, 20)
    check("two adjacent far outliers both flagged", sorted(p.idx.tolist()) == [58, 59])
    # 6) chunking: results identical for a tiny block size
    global BLOCK
    old, BLOCK = BLOCK, 7
    p_small = pass1(60, make_hav_block_fn(pair, ROAD_FACTOR), 3.0, 20)
    BLOCK = old
    check("block size does not change the answer", sorted(p_small.idx.tolist()) == sorted(p.idx.tolist()))
    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 9. CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Independent QA oracle for ro-vibe geographic outlier detection.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--input", help="the SAME input.xlsx (or .csv) you give to ro-vibe")
    p.add_argument("--ro-vibe-result", help="ro-vibe outliers (csv/xlsx/json) to compare against")
    p.add_argument("--rv-db-col", help="db column name in the ro-vibe result file (auto-detected if omitted)")
    p.add_argument("--rv-outlet-col", help="outlet column name in the ro-vibe result file")
    p.add_argument("--rv-flag-col", help="optional is_outlier column; only truthy rows count")
    p.add_argument("--compare-mode", choices=MODES, default="phase1",
                   help="which ro-vibe code path the result file came from")
    p.add_argument("--out", default="outlier_qa_report.xlsx")
    g = p.add_argument_group("algorithm parameters (defaults = ro-vibe)")
    g.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    g.add_argument("--min-sample", type=int, default=DEFAULT_MIN_SAMPLE)
    g.add_argument("--excel-min-outlets", type=int, default=DEFAULT_MIN_SAMPLE)
    g.add_argument("--road-factor", type=float, default=ROAD_FACTOR)
    g.add_argument("--knn-k", type=int, default=KNN_K)
    g.add_argument("--knn-km", type=float, default=KNN_KM)
    g.add_argument("--centroid-far-km", type=float, default=CENTROID_FAR_KM)
    g.add_argument("--centroid-road-factor", type=float, default=1.0,
                   help="factor applied to centroid distance in pass 2 (1.0 = raw haversine; UNVERIFIED)")
    g.add_argument("--lat-min", type=float, default=-90.0)
    g.add_argument("--lat-max", type=float, default=90.0)
    g.add_argument("--lon-min", type=float, default=-180.0)
    g.add_argument("--lon-max", type=float, default=180.0)
    o = p.add_argument_group("OSRM (optional - needs network access to your OSRM host)")
    o.add_argument("--osrm-url", help="full OSRM host incl. country path, e.g. https://osrm.example.com/india")
    o.add_argument("--osrm-chunk", type=int, default=100)
    o.add_argument("--osrm-timeout", type=float, default=15.0)
    o.add_argument("--osrm-workers", type=int, default=4)
    o.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed OSRM host)")
    o.add_argument("--check-unroutable", action="store_true",
                   help="flag outlets with no road nearby (river/sea) and verify ro-vibe flagged them")
    o.add_argument("--snap-threshold-m", type=float, default=500.0)
    p.add_argument("--self-test", action="store_true", help="verify this script's own maths and exit")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.input:
        raise SystemExit("--input is required (or use --self-test)")
    if a.osrm_url:
        a.osrm_url = a.osrm_url.rstrip("/")
        if a.insecure:
            import urllib3
            urllib3.disable_warnings()

    raw = load_input(a.input)
    good, excluded = split_valid(raw, a.lat_min, a.lat_max, a.lon_min, a.lon_max)
    print(f"Input: {len(raw)} outlets | valid {len(good)} | excluded {len(excluded)} | "
          f"DBs {good['db_id'].nunique()}")

    det, summ, notes = analyse(good, a)
    for n in notes:
        print("[note]", n)

    sheets: dict[str, pd.DataFrame] = {}
    exit_code = 0

    if a.ro_vibe_result:
        rv = load_rv_result(a.ro_vibe_result, a.rv_db_col, a.rv_outlet_col, a.rv_flag_col)
        cmp_df, mism = compare(det, rv, a.compare_mode, a)
        sheets["Comparison_per_DB"] = cmp_df
        sheets["Mismatched_outlets"] = mism
        fails = int((cmp_df["result"] == "FAIL").sum())
        print(f"\nCOMPARE ({a.compare_mode}): {len(cmp_df) - fails} PASS / {fails} FAIL of {len(cmp_df)} DBs")
        if fails:
            exit_code = 1
            print(cmp_df.loc[cmp_df["result"] == "FAIL",
                             ["db_id", "script_outliers", "ro_vibe_outliers", "missed_by_ro_vibe",
                              "extra_in_ro_vibe"]].to_string(index=False))

    if a.osrm_url and a.check_unroutable and not det.empty:
        un = det.loc[det["unroutable"], ["db_id", "outlet_id", "outlet_name", "lat", "lon", "snap_m"]].copy()
        if a.ro_vibe_result:
            flagged = {(d, o) for d, s in rv.items() for o in s}
            un["flagged_by_ro_vibe"] = [(d, o) in flagged for d, o in zip(un["db_id"], un["outlet_id"])]
            un["check"] = np.where(un["flagged_by_ro_vibe"], "PASS", "FAIL")
            bad = int((un["check"] == "FAIL").sum())
            print(f"\nUNROUTABLE check: {len(un)} outlets without a nearby road; "
                  f"{bad} NOT flagged by ro-vibe")
            if bad:
                exit_code = 1
        else:
            print(f"\nUNROUTABLE: {len(un)} outlets without a nearby road (no ro-vibe file to compare).")
        sheets["Unroutable_check"] = un

    sheets["Summary_per_DB"] = summ
    sheets["Outlier_details"] = det
    sheets["Excluded_outlets"] = excluded
    cfg = {k: v for k, v in vars(a).items()}
    sheets["Run_config"] = pd.DataFrame({"parameter": list(cfg), "value": [str(v) for v in cfg.values()]})
    # keep Comparison first when present
    order = [k for k in ("Comparison_per_DB", "Mismatched_outlets", "Unroutable_check") if k in sheets]
    order += ["Summary_per_DB", "Outlier_details", "Excluded_outlets", "Run_config"]
    write_report(a.out, {k: sheets[k] for k in order})

    print("\nSummary per DB:")
    cols = ["db_id", "valid_outlets", "excel_outliers", "qualifier_outliers", "phase1_outliers", "phase1_distance"]
    print(summ[cols].to_string(index=False))
    print(f"\nReport written: {os.path.abspath(a.out)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
