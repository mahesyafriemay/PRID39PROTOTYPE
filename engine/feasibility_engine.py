"""
Feasibility Gate (MRP material + RCCP kapasitas)

Input : priority_df, bom, material, supplier, capacity, params
Output: summary_df  (status LAYAK / HOLD per SKU + alasan),
        detail_df   (kebutuhan per SKU x komponen),
        material_df (saldo material + alert Ready / Shortage / Supplier ETA Risk)

Alokasi material & kapasitas dilakukan berurutan sesuai prioritas
(P1 -> P2 -> P3, Critical Ratio terkecil dulu), sehingga SKU paling kritis
mendapat material terlebih dahulu.
"""
from __future__ import annotations

import pandas as pd

from engine import config as cfg
from utils.helpers import fmt_date, fmt_dec, fmt_int

CLASS_ORDER = {"P1": 1, "P2": 2, "P3": 3}


def default_params() -> dict:
    return {
        "material_availability_pct": 100.0,   # skala stok material tersedia
        "supplier_eta_shift_days": 0,          # + = supplier mundur, - = maju
        "line_capacity_pct": 100.0,            # skala jam tersedia line
        "line_rate": cfg.LINE_RATE,
        "setup_hours": cfg.SETUP_HOURS,
        "need_date": pd.Timestamp(cfg.MATERIAL_NEED_DATE),
        "supply_confirmation": {},
    }


def priority_order(priority_df: pd.DataFrame) -> pd.DataFrame:
    df = priority_df.copy()
    df["_cls"] = df["Priority_Class"].map(CLASS_ORDER)
    return df.sort_values(["_cls", "Critical_Ratio"]).drop(columns="_cls").reset_index(drop=True)


def plan_window_hours(capacity: pd.DataFrame, capacity_pct: float) -> float:
    win = capacity[capacity["In_Plan_Window"]]
    return float((win["Shift_Hours"] - win["Planned_Downtime_Hours"]).clip(lower=0).sum()) * capacity_pct / 100


SUPPLY_STATUSES = ["Available", "Partial", "Not Available"]


def default_supply_confirmation(material: pd.DataFrame, need_date=None) -> dict:
    """Prefill konfirmasi pasokan awal dari data supplier (ETA <= tanggal butuh → Available)."""
    need = pd.Timestamp(need_date or cfg.MATERIAL_NEED_DATE)
    out = {}
    for _, m in material[material["Incoming_Qty"] > 0].iterrows():
        ok = pd.notna(m["Supplier_ETA"]) and m["Supplier_ETA"] <= need
        out[m["Material_ID"]] = {"status": "Available" if ok else "Not Available", "ready_qty": 0,
                                 "eta": m["Supplier_ETA"].strftime("%Y-%m-%d") if pd.notna(m["Supplier_ETA"]) else ""}
    return out


def prepare_material(material: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Stok efektif + pasokan siap/tidak siap, memperhitungkan konfirmasi pasokan awal (langkah 7)."""
    m = material.copy()
    need = params["need_date"]
    shift = pd.to_timedelta(params["supplier_eta_shift_days"], unit="D")
    conf = params.get("supply_confirmation") or {}
    m["Stock_Effective"] = m["Stock_Available"] * params["material_availability_pct"] / 100
    eta_eff, ready, status = [], [], []
    for _, r in m.iterrows():
        c = conf.get(r["Material_ID"])
        eta = pd.Timestamp(c["eta"]) if c and c.get("eta") else r["Supplier_ETA"]
        e = eta + shift if pd.notna(eta) else pd.NaT
        arrives = pd.notna(e) and e <= need
        inc = float(r["Incoming_Qty"])
        if r["Incoming_Qty"] <= 0:
            q, st_ = 0.0, "Stok saja"
        elif c is None:
            q, st_ = (inc if arrives else 0.0), "Belum dikonfirmasi"
        elif c["status"] == "Available":
            q, st_ = inc, "Available"
        elif c["status"] == "Partial":
            q, st_ = (inc if arrives else min(float(c.get("ready_qty") or 0), inc)), "Partial"
        else:
            q, st_ = (inc if arrives else 0.0), "Not Available"
        eta_eff.append(e)
        ready.append(q)
        status.append(st_)
    m["ETA_Effective"] = eta_eff
    m["Incoming_Ready"] = ready
    m["Incoming_Not_Ready"] = m["Incoming_Qty"] - m["Incoming_Ready"]
    m["Supply_Status"] = status
    return m


def run_feasibility_engine(priority_df, data: dict, params: dict | None = None):
    params = {**default_params(), **(params or {})}
    mat = prepare_material(data["material"], params).set_index("Material_ID")
    bom = data["bom"]
    remaining_stock = mat["Stock_Effective"].to_dict()
    remaining_incoming = mat["Incoming_Ready"].to_dict()
    gross_total = {k: 0.0 for k in mat.index}
    shortage_total = {k: 0.0 for k in mat.index}

    hours_available = plan_window_hours(data["capacity"], params["line_capacity_pct"])
    hours_left = hours_available

    summary, detail = [], []
    if priority_df is None or priority_df.empty:
        return pd.DataFrame(columns=["SKU_ID", "Status"]), pd.DataFrame(), _material_summary(mat, gross_total, shortage_total, remaining_stock, remaining_incoming, params)

    for _, r in priority_order(priority_df).iterrows():
        qty = float(r["Recommended_Qty"])
        comps = bom[bom["SKU_ID"] == r["SKU_ID"]]
        lines, short_msgs = [], []
        req_txt, avail_txt, inc_txt, eta_txt = [], [], [], []
        for _, b in comps.iterrows():
            mid = b["Material_ID"]
            m = mat.loc[mid]
            gross = qty * float(b["Qty_per_Unit"])
            avail = remaining_stock[mid]
            inc = remaining_incoming[mid]
            net_short = max(0.0, gross - avail - inc)
            gross_total[mid] += gross
            lines.append({
                "SKU_ID": r["SKU_ID"], "Material_ID": mid, "Material_Name": m["Material_Name"],
                "UoM": m["UoM"], "Qty_per_Unit": b["Qty_per_Unit"], "Gross_Requirement": gross,
                "Available_Stock": avail, "Ready_Incoming": inc,
                "Incoming_Not_Ready": m["Incoming_Not_Ready"], "Supplier_ETA": m["ETA_Effective"],
                "Supplier_ID": m["Supplier_ID"], "Net_Shortage": net_short,
            })
            req_txt.append(f"{m['Material_Name']}: {fmt_int(gross)} {m['UoM']}")
            avail_txt.append(f"{fmt_int(avail)} {m['UoM']}")
            inc_txt.append(f"{fmt_int(inc)} siap" + (f" (+{fmt_int(m['Incoming_Not_Ready'])} belum siap)" if m["Incoming_Not_Ready"] > 0 else ""))
            if pd.notna(m["ETA_Effective"]):
                eta_txt.append(f"{m['Material_Name'].split(' ')[0]} {fmt_date(m['ETA_Effective'])}")
            if net_short > 0:
                msg = f"Material shortage — {m['Material_Name']} kurang {fmt_int(net_short)} {m['UoM']}"
                if m["Incoming_Not_Ready"] > 0:
                    msg += f" (pasokan {fmt_int(m['Incoming_Not_Ready'])} {m['UoM']} ETA {fmt_date(m['ETA_Effective'])} belum siap)"
                short_msgs.append(msg)

        prod_hours = qty / params["line_rate"] + params["setup_hours"]
        mat_ok = not short_msgs
        cap_ok = prod_hours <= hours_left + 1e-9

        if mat_ok and cap_ok:
            status, reason = "LAYAK", "Material & kapasitas tersedia"
            for ln in lines:  # konsumsi material: stok dulu, lalu pasokan siap
                use_stock = min(ln["Gross_Requirement"], remaining_stock[ln["Material_ID"]])
                remaining_stock[ln["Material_ID"]] -= use_stock
                remaining_incoming[ln["Material_ID"]] -= ln["Gross_Requirement"] - use_stock
            hours_left -= prod_hours
        else:
            status = "HOLD"
            reasons = list(short_msgs)
            if not cap_ok:
                reasons.append(f"Capacity unavailable — stream Liquid penuh pada planning horizon "
                               f"(butuh {fmt_dec(prod_hours, 1)} jam, sisa {fmt_dec(hours_left, 1)} jam)")
            reason = "; ".join(reasons) + "."
            for ln in lines:
                shortage_total[ln["Material_ID"]] += ln["Net_Shortage"]

        for ln in lines:
            ln["Status"] = status
        detail.extend(lines)
        summary.append({
            "SKU_ID": r["SKU_ID"], "Nama_Produk": r["Nama_Produk"], "Kategori": r["Kategori"],
            "Priority_Class": r["Priority_Class"], "Critical_Ratio": r["Critical_Ratio"],
            "Recommended_Qty": qty,
            "BOM_Requirement": "; ".join(req_txt),
            "Available_Material": "; ".join(avail_txt),
            "Incoming_Supply": "; ".join(inc_txt),
            "Supplier_ETA": "; ".join(eta_txt) if eta_txt else "-",
            "Required_Hours": prod_hours,
            "Capacity_Availability": f"Butuh {fmt_dec(prod_hours, 1)} jam / sisa {fmt_dec(hours_left if status == 'HOLD' else hours_left + prod_hours, 1)} jam",
            "Changeover_Requirement": f"Setup {fmt_dec(params['setup_hours'], 1)} jam (+{fmt_dec(cfg.CATEGORY_CHANGEOVER_HOURS, 1)} jam jika ganti kategori)",
            "Material_Shortage": "; ".join(short_msgs) if short_msgs else "0",
            "Material_OK": mat_ok, "Capacity_OK": cap_ok,
            "Hold_Type": "-" if status == "LAYAK" else ("Material + Kapasitas" if not (mat_ok or cap_ok)
                                                        else ("Material" if not mat_ok else "Kapasitas")),
            "Status": status, "Reason": reason,
        })

    summary_df = pd.DataFrame(summary)
    detail_df = pd.DataFrame(detail)
    summary_df.attrs["hours_available"] = hours_available
    summary_df.attrs["hours_left"] = hours_left
    return summary_df, detail_df, _material_summary(mat, gross_total, shortage_total, remaining_stock, remaining_incoming, params)


def _material_summary(mat, gross_total, shortage_total, remaining_stock, remaining_incoming, params):
    rows = []
    for mid, m in mat.iterrows():
        late_supply = m["Incoming_Not_Ready"] > 0 and gross_total[mid] > 0
        if shortage_total[mid] > 0:
            alert = "Material Shortage"
        elif late_supply or (m["Supplier_Confirmed"] != "Ya" and m["Incoming_Qty"] > 0 and gross_total[mid] > 0):
            alert = "Supplier ETA Risk"
        else:
            alert = "Material Ready"
        rows.append({
            "Material_ID": mid, "Material_Name": m["Material_Name"], "UoM": m["UoM"],
            "Stock_Available": m["Stock_Effective"], "Incoming_Qty": m["Incoming_Qty"],
            "Supplier_ETA": m["ETA_Effective"], "Supplier_ID": m["Supplier_ID"],
            "Supplier_Confirmed": m["Supplier_Confirmed"], "Supply_Status": m["Supply_Status"],
            "Gross_Requirement": gross_total[mid], "Shortage": shortage_total[mid],
            "Incoming_Ready": m["Incoming_Ready"],
            "Remaining_After_Plan": remaining_stock[mid] + remaining_incoming[mid], "Alert": alert,
        })
    return pd.DataFrame(rows)
