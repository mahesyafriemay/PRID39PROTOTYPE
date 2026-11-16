"""
Actual Engine — langkah 16–18 CFD (eksekusi simulasi → hasil aktual → evaluasi).

Hasil aktual dibangkitkan deterministik (hash SKU + siklus) agar demo dapat
diulang. Nilai aktual disimpan TERPISAH dari nilai rencana (kolom Planned_* vs Actual_*).
Tidak ada koneksi ke sistem eksekusi / SAP.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pandas as pd

from engine import config as cfg

ADHERENCE_TOLERANCE_HOURS = 4.0


def _h(key: str) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16)


def generate_actuals(schedule_records: list, bom: pd.DataFrame, cycle: int = 1) -> list[dict]:
    rows = []
    for r in schedule_records:
        h = _h(f"{r['SKU_ID']}-{cycle}")
        rows.append({"r": r, "h": h, "issue": h % 5 == 0})
    if rows and not any(x["issue"] for x in rows):          # minimal satu isu agar evaluasi bermakna
        max(rows, key=lambda x: x["r"]["Quantity"])["issue"] = True

    out = []
    for x in rows:
        r, h, issue = x["r"], x["h"], x["issue"]
        yld = 0.955 + (h % 50) / 1000                       # 95,5% – 100,4%
        delay_h = float((h // 7) % 6)                         # 0 – 5 jam
        scrap = 0.01 + (h % 3) / 100                          # 1 – 3%
        issue_txt = "-"
        if issue:
            yld -= 0.03
            delay_h += 6
            scrap += 0.03
            issue_txt = "Reject kemasan / material tertahan QC incoming"
        planned_qty = float(r["Quantity"])
        actual_qty = round(planned_qty * yld / 10) * 10
        pack = bom[(bom["SKU_ID"] == r["SKU_ID"]) & (bom["Qty_per_Unit"] == 1)]
        planned_use = planned_qty * (1 if len(pack) else 0)
        actual_use = round(actual_qty * (1 + scrap))
        a_finish = pd.Timestamp(r["Finish_Production"]) + timedelta(hours=delay_h)
        a_eta = pd.Timestamp(r["ETA_NDC"]) + timedelta(hours=delay_h)
        target = pd.Timestamp(r["Target_Date"])
        a_delay = max(0.0, (a_eta - target).total_seconds() / 86400)
        out.append({
            "Sequence": r["Sequence"], "SKU_ID": r["SKU_ID"], "Nama_Produk": r["Nama_Produk"],
            "Priority_Class": r["Priority_Class"],
            "Planned_Qty": planned_qty, "Actual_Qty": float(actual_qty), "Qty_Achievement": actual_qty / planned_qty,
            "Planned_Finish": pd.Timestamp(r["Finish_Production"]), "Actual_Finish": a_finish,
            "Finish_Deviation_Hours": delay_h,
            "Planned_ETA": pd.Timestamp(r["ETA_NDC"]), "Actual_ETA": a_eta, "Target_Date": target,
            "Planned_Late": bool(r["Late"]), "Actual_Late": a_eta > target, "Actual_Delay_Days": a_delay,
            "Planned_Material_Use": planned_use, "Actual_Material_Use": float(actual_use),
            "Material_Variance": (actual_use / actual_qty - 1) if actual_qty else 0.0,   # kemasan terbuang per unit baik
            "Material_Issue": issue_txt,
            "Adherent": delay_h <= ADHERENCE_TOLERANCE_HOURS,
        })
    return out


def actual_kpis(actual_records: list, buffer_df: pd.DataFrame, hold_p1: int, revision_events: int) -> dict:
    a = pd.DataFrame(actual_records)
    buf = buffer_df.set_index("SKU_ID")
    produced = dict(zip(a["SKU_ID"], a["Actual_Qty"])) if len(a) else {}
    pos = buf["NIP"] + buf.index.map(produced).fillna(0).astype(float)
    over = int((pos > buf["Max_Level"] + buf["Batch_Size"]).sum())
    late = int(a["Actual_Late"].sum()) if len(a) else 0
    return {
        "stockout_risk": late + hold_p1,
        "overstock_risk": over,
        "delayed_orders": late,
        "total_delay_days": float(a["Actual_Delay_Days"].sum()) if len(a) else 0.0,
        "inventory_value": float((pos.clip(lower=0) * buf["COGS"]).sum()),
        "revisions": revision_events,
        "qty_achievement": float(a["Actual_Qty"].sum() / a["Planned_Qty"].sum()) if len(a) else 0.0,
        "material_issues": int((a["Material_Issue"] != "-").sum()) if len(a) else 0,
        "schedule_adherence": float(a["Adherent"].mean()) if len(a) else 0.0,
        "planned_qty": float(a["Planned_Qty"].sum()) if len(a) else 0.0,
        "actual_qty": float(a["Actual_Qty"].sum()) if len(a) else 0.0,
    }


def plan_kpis_for_compare(plan_kpi: dict, revision_events: int) -> dict:
    """KPI rencana dalam format yang sama dengan actual_kpis."""
    return {**plan_kpi, "revisions": revision_events, "qty_achievement": 1.0, "material_issues": 0,
            "schedule_adherence": 1.0}


PVA_ROWS = [
    ("Stockout", "stockout_risk", "int", True),
    ("Overstock", "overstock_risk", "int", True),
    ("Delay (order)", "delayed_orders", "int", True),
    ("Inventory value", "inventory_value", "rp", True),
    ("Schedule revision", "revisions", "int", True),
    ("Quantity achievement", "qty_achievement", "pct", False),
    ("Material issue", "material_issues", "int", True),
    ("Schedule adherence", "schedule_adherence", "pct", False),
]


def plan_vs_actual(plan: dict, actual: dict) -> pd.DataFrame:
    rows = []
    for lbl, k, kind, lower_better in PVA_ROWS:
        p, a = float(plan.get(k, 0) or 0), float(actual.get(k, 0) or 0)
        d = a - p
        verdict = "Sesuai" if abs(d) < 1e-9 else ("Lebih baik" if (d < 0) == lower_better else "Deviasi")
        rows.append({"KPI": lbl, "key": k, "kind": kind, "Plan": p, "Actual": a, "Deviasi": d, "Status": verdict})
    return pd.DataFrame(rows)


def replanning_needed(actual_kpi: dict, triggers: list[dict]) -> bool:
    th = cfg.KPI_THRESHOLDS
    return (any(t["on"] for t in triggers) or actual_kpi["delayed_orders"] >= th["delayed_orders"]
            or actual_kpi["material_issues"] > 0)
