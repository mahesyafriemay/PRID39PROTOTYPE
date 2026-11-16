"""H04 — Feasibility Gate (PPC, Supply Planner) · CFD langkah 6–9."""
import altair as alt
import pandas as pd
import streamlit as st

from engine import workflow_engine as wfe
from engine.feasibility_engine import SUPPLY_STATUSES
from utils import state
from utils.helpers import fmt_date, fmt_dec, fmt_int
from utils.styles import (AMBER, NAVY, ORANGE, RED, TEAL, W, alert_row, chart, html_table, kpi_card, meter,
                          section, stepper)

ALERT_SCALE = {"domain": ["Material Shortage", "Supplier ETA Risk", "Material Ready"], "range": [RED, AMBER, TEAL]}


def render():
    state.header("Feasibility", "6 Kebutuhan material → 7 Konfirmasi pasokan awal → 8 Cek kapasitas → Gate LAYAK / HOLD.")
    wf = state.wf()
    role = st.session_state.role
    res = state.active_result()
    feas, detail, mat = res["feasibility"], res["feasibility_detail"], res["material"]
    if feas.empty:
        st.info("Tidak ada SKU di antrean produksi (semua NIP > ROP).")
        return

    n_layak, n_hold = int((feas["Status"] == "LAYAK").sum()), int((feas["Status"] == "HOLD").sum())
    hrs_av = feas.attrs.get("hours_available", 0.0)
    used = hrs_av - feas.attrs.get("hours_left", 0.0)
    started = wf["status"] != wfe.DRAFT
    stepper([
        ("6 · Kebutuhan material", f"{len(detail)} baris BOM dihitung", "done"),
        ("7 · Pasokan awal", "Terkonfirmasi" if wf["initial_supply_confirmed"] else "Menunggu Supply Planner",
         "done" if wf["initial_supply_confirmed"] else ("active" if started else "todo")),
        ("8 · Cek kapasitas", f"{fmt_dec(used, 1)} / {fmt_dec(hrs_av, 1)} jam", "done"),
        ("Gate", f"{n_layak} LAYAK · {n_hold} HOLD",
         ("blocked" if n_layak == 0 else ("done" if wf["initial_supply_confirmed"] else "active"))),
    ])

    c = st.columns([1, 1, 1.4])
    with c[0]:
        kpi_card("LAYAK → H05", n_layak, "masuk sequencing", TEAL, "check")
    with c[1]:
        kpi_card("HOLD / Eskalasi", n_hold, "tidak masuk sequencing", RED, "stop")
    with c[2], st.container(border=True):
        meter("Kapasitas stream Liquid (16–20 Nov)", used, hrs_av, ORANGE if used / max(hrs_av, 1) > 0.85 else TEAL,
              f"{fmt_dec(used, 1)} / {fmt_dec(hrs_av, 1)} jam")
        n_ready = int((mat["Alert"] == "Material Ready").sum())
        meter("Material siap", n_ready, len(mat), TEAL, f"{n_ready}/{len(mat)} item")

    _initial_supply_panel(wf, role, mat)

    # Gate --------------------------------------------------------------------
    section("Hasil Feasibility Gate", "alokasi material & jam line: P1 → P3, Critical Ratio terkecil dulu")
    tbl = feas[["SKU_ID", "Priority_Class", "Recommended_Qty", "Required_Hours", "Hold_Type", "Status"]]
    html_table(tbl, badges=("Priority_Class", "Hold_Type", "Status"), key_col="SKU_ID",
               fmt={"Recommended_Qty": fmt_int, "Required_Hours": lambda v: f"{fmt_dec(v, 1)} jam"},
               labels={"SKU_ID": "SKU", "Priority_Class": "Kelas", "Recommended_Qty": "Qty", "Required_Hours": "Kapasitas",
                       "Hold_Type": "Penyebab HOLD"})
    hold = feas[feas["Status"] == "HOLD"]
    for _, h in hold.iterrows():
        alert_row(f"HOLD / Eskalasi · {h['SKU_ID']} {h['Nama_Produk']} ({h['Priority_Class']})", h["Reason"], RED, "stop")

    if role == state.PPC and wf["status"] in (wfe.PLAN_REVIEW, wfe.HOLD, wfe.SCENARIO_REVIEW, wfe.REVISION_REQUIRED):
        b1, b2, _ = st.columns([1.2, 1.2, 2])
        if len(hold) and b1.button("⚠ Eskalasi HOLD → H06 What-if", type="primary", **W):
            if wf["status"] in (wfe.PLAN_REVIEW, wfe.HOLD):
                ok = state.transition(wfe.evaluate_whatif, flash="Eskalasi HOLD: perbaiki rencana di What-if.", goto="What-if")
            else:
                state.go_to("What-if")
                ok = True
            if ok:
                st.rerun()
        if n_layak and b2.button("Lanjut → H05 Sequencing", **W):
            state.go_to("Sequencing & ETA")
            st.rerun()

    # Coverage & BOM -----------------------------------------------------------
    left, right = st.columns([1.2, 1])
    with left:
        section("6 · Coverage Material", "(stok + pasokan siap) ÷ kebutuhan rencana")
        m = mat[mat["Gross_Requirement"] > 0].copy()
        m["Coverage"] = ((m["Stock_Available"] + m["Incoming_Ready"]) / m["Gross_Requirement"]).clip(upper=2.0)
        bars = alt.Chart(m).mark_bar(size=14, cornerRadiusEnd=4).encode(
            y=alt.Y("Material_Name:N", sort="x", title=None), x=alt.X("Coverage:Q", title=None, axis=alt.Axis(format="%")),
            color=alt.Color("Alert:N", scale=alt.Scale(**ALERT_SCALE), legend=alt.Legend(title=None)),
            tooltip=["Material_Name", alt.Tooltip("Coverage:Q", format=".0%"), alt.Tooltip("Gross_Requirement:Q", format=",.0f"),
                     alt.Tooltip("Stock_Available:Q", format=",.0f"), alt.Tooltip("Incoming_Ready:Q", format=",.0f")])
        rule = alt.Chart(pd.DataFrame({"x": [1.0]})).mark_rule(color=NAVY, strokeDash=[5, 4]).encode(x="x:Q")
        st.altair_chart(chart(bars + rule, 30 * len(m) + 50), **W)
    with right:
        section("Detail BOM per SKU")
        sid = st.selectbox("SKU", feas["SKU_ID"], label_visibility="collapsed",
                           format_func=lambda s: f"{s} · {feas.set_index('SKU_ID').loc[s, 'Status']}")
        d = detail[detail["SKU_ID"] == sid][["Material_Name", "Gross_Requirement", "Available_Stock", "Ready_Incoming", "Net_Shortage", "UoM"]]
        html_table(d, key_col="Material_Name", sub_cols=("UoM",),
                   fmt={k: fmt_int for k in ("Gross_Requirement", "Available_Stock", "Ready_Incoming", "Net_Shortage")},
                   labels={"Material_Name": "Komponen", "Gross_Requirement": "Bruto", "Available_Stock": "Stok*",
                           "Ready_Incoming": "Siap*", "Net_Shortage": "Shortage"})
        st.caption("*sisa setelah alokasi ke SKU prioritas lebih tinggi")


def _initial_supply_panel(wf, role, mat):
    """Langkah 7 — Supply Planner: Available / Partial / Not Available + ETA supplier."""
    section("7 · Konfirmasi Pasokan Awal", "Supply Planner")
    sup = state.get_data()["supplier"].set_index("Supplier_ID")["Supplier_Name"]
    conf = state.supply_confirmation()
    m = mat[mat["Incoming_Qty"] > 0].copy()
    m["Supplier"] = m["Supplier_ID"].map(sup)
    allowed = (wfe.FEASIBILITY_CHECK, wfe.HOLD, wfe.PLAN_REVIEW, wfe.SCENARIO_REVIEW, wfe.REVISION_REQUIRED)

    if role == state.SUPPLY and wf["status"] in allowed:
        ed = pd.DataFrame({
            "Material_ID": m["Material_ID"], "Material": m["Material_Name"], "Supplier": m["Supplier"],
            "Incoming": m["Incoming_Qty"].astype(int),
            "Status": [conf.get(i, {}).get("status", "Not Available") for i in m["Material_ID"]],
            "Qty siap (Partial)": [int(conf.get(i, {}).get("ready_qty", 0) or 0) for i in m["Material_ID"]],
            "ETA supplier": [pd.Timestamp(conf.get(i, {}).get("eta") or e).date() if pd.notna(e) else None
                             for i, e in zip(m["Material_ID"], m["Supplier_ETA"])],
        })
        edited = st.data_editor(
            ed, hide_index=True, key="initial_supply_editor", disabled=["Material_ID", "Material", "Supplier", "Incoming"],
            column_config={"Material_ID": None,
                           "Status": st.column_config.SelectboxColumn("Status", options=SUPPLY_STATUSES, required=True),
                           "Qty siap (Partial)": st.column_config.NumberColumn(min_value=0, step=1000, format="%d"),
                           "ETA supplier": st.column_config.DateColumn(format="DD MMM YYYY")}, **W)
        if st.button("✔ Konfirmasi Pasokan Awal", type="primary"):
            new_conf = {r["Material_ID"]: {"status": r["Status"], "ready_qty": int(r["Qty siap (Partial)"] or 0),
                                           "eta": str(r["ETA supplier"]) if r["ETA supplier"] else ""}
                        for r in edited.to_dict("records")}
            if state.transition(wfe.confirm_initial_supply, new_conf, state.gate_fn,
                                flash="Pasokan awal dikonfirmasi — feasibility gate dihitung ulang."):
                st.rerun()
    else:
        t = m[["Material_Name", "Supplier", "Incoming_Qty", "Supplier_ETA", "Supply_Status", "Incoming_Ready"]].copy()
        html_table(t, badges=("Supply_Status",), key_col="Material_Name",
                   fmt={"Incoming_Qty": fmt_int, "Supplier_ETA": fmt_date, "Incoming_Ready": fmt_int},
                   labels={"Material_Name": "Material", "Incoming_Qty": "Incoming", "Supplier_ETA": "ETA",
                           "Supply_Status": "Konfirmasi", "Incoming_Ready": "Siap dipakai"})
        if role == state.SUPPLY:
            st.caption("Konfirmasi pasokan awal hanya dapat diubah sebelum rencana diajukan ke approval.")
        elif not wf["initial_supply_confirmed"]:
            st.caption("Belum dikonfirmasi Supply Planner — sementara memakai ETA dari data supplier.")
