"""
Quality Engine — elemen sistem manajemen mutu yang selaras ISO 9001:2015
(bukan klaim sertifikasi):

- Sasaran mutu & pengukuran kinerja        (klausul 6.2, 9.1)
- Ketidaksesuaian & tindakan korektif (NC/CAPA)  (klausul 8.7, 10.2)
- Pemicu replanning / pengendalian perubahan     (klausul 6.3, 8.5.6)
"""
from __future__ import annotations

import pandas as pd

from engine import config as cfg

PDCA = {
    "PLAN": ["H01", "H02", "H03", "H04", "H05", "H06"],
    "DO": ["H07"],
    "CHECK": ["H08"],
    "ACT": ["H06"],
}


def _check(value, target, op):
    return value <= target if op == "<=" else value >= target


def quality_objectives(kpi: dict, revisions: int, data_issues: int) -> pd.DataFrame:
    actual = {
        "stockout_risk": kpi["stockout_risk"],
        "delayed_orders": kpi["delayed_orders"],
        "feasibility_rate": (kpi["layak"] / kpi["eligible"]) if kpi["eligible"] else 1.0,
        "overstock_risk": kpi["overstock_risk"],
        "revisions": revisions,
        "data_issues": data_issues,
    }
    rows = []
    for key, o in cfg.QUALITY_OBJECTIVES.items():
        v = actual[key]
        rows.append({"key": key, "Sasaran": o["label"], "Indikator": o["indikator"],
                     "Target": (("≤ " if o["op"] == "<=" else "≥ ") + (f"{o['target']:.0%}" if key == "feasibility_rate" else f"{o['target']}")),
                     "Aktual": f"{v:.0%}" if key == "feasibility_rate" else f"{v:g}",
                     "value": v, "target": o["target"], "op": o["op"],
                     "Status": "Tercapai" if _check(v, o["target"], o["op"]) else "Tidak tercapai"})
    return pd.DataFrame(rows)


def nonconformities(res: dict, actual: list | None = None) -> pd.DataFrame:
    """HOLD, order terlambat, dan isu aktual dicatat sebagai ketidaksesuaian + tindakan korektif."""
    rows = []
    feas, sch = res["feasibility"], res["schedule"]
    n = 1
    if not feas.empty:
        for _, h in feas[feas["Status"] == "HOLD"].iterrows():
            kind = "Material" if not h["Material_OK"] else "Kapasitas"
            action = ("Eskalasi supplier / percepat ETA, alokasi ulang material, atau turunkan qty batch"
                      if kind == "Material" else "Tambah shift / geser slot, atau pecah batch")
            rows.append({"NC_ID": f"NC-{n:03d}", "SKU": h["SKU_ID"], "Kategori": kind,
                         "Temuan": h["Reason"].rstrip("."), "Tindakan korektif": action,
                         "PIC": "Supply Planner" if kind == "Material" else "Leader Produksi",
                         "Kelas": h["Priority_Class"], "Status": "Open"})
            n += 1
    if actual:
        for r in actual:
            if r["Material_Issue"] != "-":
                rows.append({"NC_ID": f"NC-{n:03d}", "SKU": r["SKU_ID"], "Kategori": "Material",
                             "Temuan": f"Aktual: {r['Material_Issue']} (achievement {r['Qty_Achievement']:.0%})",
                             "Tindakan korektif": "Inspeksi incoming material, klaim supplier, tambah batch susulan",
                             "PIC": "Supply Planner", "Kelas": r["Priority_Class"], "Status": "Open"})
                n += 1
            if r["Actual_Late"]:
                rows.append({"NC_ID": f"NC-{n:03d}", "SKU": r["SKU_ID"], "Kategori": "Delay",
                             "Temuan": f"Aktual ETA melewati target {r['Actual_Delay_Days']:.1f} hari",
                             "Tindakan korektif": "Percepat QC release / transfer stok antar-DC",
                             "PIC": "PPC", "Kelas": r["Priority_Class"], "Status": "Open"})
                n += 1
    elif not sch.empty:
        for _, r in sch[sch["Late"]].iterrows():
            rows.append({"NC_ID": f"NC-{n:03d}", "SKU": r["SKU_ID"], "Kategori": "Delay",
                         "Temuan": f"ETA NDC melewati target {r['Delay_Days']:.1f} hari",
                         "Tindakan korektif": "Majukan urutan / percepat QC release, alokasi stok DC lain",
                         "PIC": "PPC", "Kelas": r["Priority_Class"], "Status": "Open"})
            n += 1
    return pd.DataFrame(rows, columns=["NC_ID", "SKU", "Kategori", "Temuan", "Tindakan korektif", "PIC", "Kelas", "Status"])


def replanning_triggers(res: dict, data: dict, feedback_count: int = 0, actual_kpi: dict | None = None) -> list[dict]:
    """Langkah 18–19: pemicu replanning dari rencana aktif dan (bila ada) hasil aktual."""
    p, mat, k, buf = res["params"], res["material"], res["kpi"], res["buffer"]
    last = data["weekly_demand"].sort_values("Week_Start").groupby("SKU_ID").tail(1).set_index("SKU_ID")["Demand_Units"]
    surge = [s for s, b in zip(buf["SKU_ID"], buf["Baseline_B"]) if s in last and last[s] > 1.25 * b]
    n_short = int((mat["Alert"] == "Material Shortage").sum())
    n_eta = int((mat["Alert"] == "Supplier ETA Risk").sum())
    late_supply = bool((((mat["Incoming_Qty"] - mat["Incoming_Ready"]) > 0) & (mat["Gross_Requirement"] > 0)).any())
    cap_hold = (not res["feasibility"].empty) and bool((~res["feasibility"]["Capacity_OK"]).any())
    a = actual_kpi or {}
    mat_issue = int(a.get("material_issues", 0))
    late = int(a.get("delayed_orders", k["delayed_orders"]))
    th = cfg.KPI_THRESHOLDS
    stockout = a.get("stockout_risk", k["stockout_risk"])
    over = a.get("overstock_risk", k["overstock_risk"])
    return [
        {"name": "Material shortage", "on": feedback_count > 0 or n_short > 0 or mat_issue > 0 or p["material_availability_pct"] < 100,
         "detail": f"{n_short} shortage · {mat_issue} isu aktual · {feedback_count} feedback"},
        {"name": "Supplier delay", "on": p["supplier_eta_shift_days"] > 0 or n_eta > 0 or late_supply,
         "detail": f"{n_eta} ETA risk · shift {p['supplier_eta_shift_days']:+d} hari"},
        {"name": "Demand spike", "on": p["demand_multiplier"] > 1 or p["campaign_uplift_multiplier"] > 1 or bool(surge),
         "detail": f"×{p['demand_multiplier']:.2f}" + (f" · {', '.join(surge)}" if surge else " · stabil")},
        {"name": "Capacity issue", "on": p["line_capacity_pct"] < 100 or cap_hold,
         "detail": f"Line {p['line_capacity_pct']:.0f}%" + (" · HOLD kapasitas" if cap_hold else "")},
        {"name": "Late order", "on": late > 0, "detail": f"{late} order ETA > target" + (" (aktual)" if a else "")},
        {"name": "KPI threshold", "on": stockout >= th["stockout_risk"] or late >= th["delayed_orders"] or over >= th["overstock"],
         "detail": f"stockout {stockout} · delay {late} · overstock {over}"},
    ]
