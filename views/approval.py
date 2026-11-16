"""H07 — Decision Review & Approval (PPC, Supply Planner, Leader Produksi) · CFD langkah 13–17."""
from datetime import date

import pandas as pd
import streamlit as st

from engine import workflow_engine as wfe
from engine.actual_engine import actual_kpis, generate_actuals, replanning_needed
from engine.quality_engine import replanning_triggers
from utils import state
from utils.helpers import fmt_date, fmt_dt, fmt_int, fmt_rp
from utils.styles import (GREEN, NAVY, ORANGE, RED, TEAL, alert_row, banner, html_table, kpi_card, section, sim_notice,
                          stepper)

ORDER = [wfe.MATERIAL_FINAL_CONFIRMATION, wfe.MATERIAL_FINAL_CONFIRMED, wfe.APPROVED, wfe.READY_FOR_SAP_RELEASE,
         wfe.RELEASED_SIMULATION, wfe.MONITORING]


def _steps(wf):
    s = wf["status"]
    idx = ORDER.index(s) if s in ORDER else (len(ORDER) if s == wfe.REPLANNING_REQUIRED else -1)

    def st_(i, done_txt, active_txt):
        if s == wfe.REVISION_REQUIRED and i == (0 if (wf["material_final"] or {}).get("decision") == "Need Revision" else 1):
            return ("Need revision", "blocked")
        if idx > i:
            return (done_txt, "done")
        if idx == i:
            return (active_txt, "active")
        return ("—", "todo")
    steps = [("13 · Material final", "Confirmed", "Menunggu Supply Planner"),
             ("14 · Leader approval", "Approved", "Menunggu Leader"),
             ("15 · Release (simulasi)", "Ready for SAP", "Menunggu PPC"),
             ("16 · Simpan & eksekusi", "Executed (sim)", "Menunggu PPC"),
             ("17 · Hasil aktual", "Tercatat", "Menunggu PPC")]
    return [(t, *st_(i, d, a)) for i, (t, d, a) in enumerate(steps)]


def _signoff(wf):
    """Lembar pengesahan revisi aktif (ISO 9001 7.5)."""
    rev = f"Rev.{wf['revision']:02d}"
    last = {}
    for h in wf["history"]:
        if h["Siklus"] == wf["cycle"] and h["Revisi"] == rev:
            last[h["Role"]] = h
    rows = []
    for role, fungsi in ((state.PPC, "Disusun"), (state.SUPPLY, "Diverifikasi material"), (state.LEADER, "Disetujui")):
        h = last.get(role)
        rows.append({"Peran": role.split(" /")[0], "Fungsi": fungsi, "Aksi terakhir": h["Aksi"] if h else "Menunggu",
                     "Waktu": h["Waktu"][11:16] if h else "—"})
    return pd.DataFrame(rows)


def render():
    state.header("Decision Review", "13 Material final → 14 Leader approve/revisi → 15 Rilis → 16 Eksekusi → 17 Hasil aktual.")
    wf = state.wf()
    role = st.session_state.role
    s = wf["status"]

    color = {"good": GREEN, "bad": RED, "active": ORANGE}.get(wfe.STATUS_INFO[s][2], NAVY)
    banner(f"{s.replace('_', ' ')} — {wfe.label(s)}", color, "check" if color == GREEN else ("alert" if color == RED else "doc"))
    stepper(_steps(wf))

    plan = pd.DataFrame(wf["active_schedule"] or [])
    if plan.empty:
        st.info("Belum ada proposed schedule aktif. PPC: review di **H05** (Accept Proposed Plan) atau pilih skenario di **H06**.")
        _audit(wf)
        return
    kpi = wf["active_kpi"]

    c = st.columns(4)
    with c[0]:
        kpi_card("Dokumen rencana", wfe.revision_label(wf), f"Skenario {wf['active_scenario']}", NAVY, "doc")
    with c[1]:
        kpi_card("Order dijadwalkan", len(plan), f"{fmt_int(plan['Quantity'].sum())} unit", TEAL, "list")
    with c[2]:
        kpi_card("HOLD · Late", f"{kpi['hold']} · {kpi['delayed_orders']}", "tidak dijadwalkan · ETA lewat", RED, "alert")
    with c[3]:
        kpi_card("Schedule revision", wf["revision_events"], fmt_rp(wf["revision_events"] * 2_500_000) + " biaya revisi", ORANGE, "refresh")

    left, right = st.columns([1.7, 1])
    with left:
        section("Proposed Schedule (aktif)", f"skenario {wf['active_scenario']} · {wfe.revision_label(wf)}")
        t = plan[["Sequence", "SKU_ID", "Nama_Produk", "Priority_Class", "Quantity", "Start_Time", "ETA_NDC", "Status_Delay"]]
        html_table(t, badges=("Priority_Class", "Status_Delay"), key_col="SKU_ID", sub_cols=("Nama_Produk",),
                   fmt={"Quantity": fmt_int, "Start_Time": fmt_dt, "ETA_NDC": fmt_dt},
                   labels={"Sequence": "#", "SKU_ID": "SKU", "Nama_Produk": "Produk", "Priority_Class": "Kelas",
                           "Quantity": "Qty", "Start_Time": "Start", "ETA_NDC": "ETA NDC", "Status_Delay": "Status"})
    with right:
        section("Lembar Pengesahan")
        html_table(_signoff(wf), key_col="Peran", sub_cols=("Fungsi", "Waktu"))
        section(f"Aksi · {role.split(' /')[0]}")
        with st.container(border=True):
            if role == state.SUPPLY:
                _supply_panel(wf, plan)
            elif role == state.LEADER:
                _leader_panel(wf)
            else:
                _ppc_panel(wf, plan)
    _audit(wf)


def _material_table(plan):
    res = state.result_for(state.wf()["active_scenario"])
    det = res["feasibility_detail"]
    det = det[det["SKU_ID"].isin(plan["SKU_ID"])]
    if det.empty:
        return pd.DataFrame()
    mat = res["material"].set_index("Material_ID")
    agg = det.groupby(["Material_ID", "Material_Name", "UoM"], as_index=False)[["Gross_Requirement", "Net_Shortage"]].sum()
    agg["Available"] = agg["Material_ID"].map(mat["Stock_Available"])
    agg["Incoming"] = agg["Material_ID"].map(mat["Incoming_Qty"])
    agg["Supplier_ETA"] = agg["Material_ID"].map(mat["Supplier_ETA"])
    return agg[["Material_ID", "Material_Name", "Gross_Requirement", "Available", "Incoming", "Supplier_ETA", "Net_Shortage", "UoM"]]


def _supply_panel(wf, plan):
    if wf["status"] != wfe.MATERIAL_FINAL_CONFIRMATION:
        st.caption("Tidak ada konfirmasi material final yang menunggu.")
        return
    agg = _material_table(plan)
    st.markdown("**Kebutuhan material rencana aktif**")
    html_table(agg.drop(columns="Material_ID"), key_col="Material_Name", sub_cols=("UoM",),
               fmt={"Gross_Requirement": fmt_int, "Available": fmt_int, "Incoming": fmt_int, "Supplier_ETA": fmt_date,
                    "Net_Shortage": fmt_int},
               labels={"Material_Name": "Material", "Gross_Requirement": "Kebutuhan", "Supplier_ETA": "ETA", "Net_Shortage": "Shortage"})
    decision = st.radio("Keputusan", ["Confirm Final Material", "Need Revision"], horizontal=True)
    if decision == "Confirm Final Material":
        note = st.text_input("Catatan (opsional)")
        if st.button("✔ Confirm Final Material", type="primary"):
            if state.transition(wfe.confirm_final_material, note, flash="Material final dikonfirmasi — giliran Leader Produksi."):
                st.rerun()
    else:
        names = dict(zip(agg["Material_Name"], agg["Material_ID"]))
        reason = st.text_input("Reason (wajib)")
        material = st.selectbox("Affected material (wajib)", list(names))
        eta = st.date_input("Updated ETA (wajib)", value=None, min_value=date(2026, 11, 1))
        note = st.text_area("Note (wajib)")
        if st.button("↩ Need Revision", type="primary"):
            conf = dict(state.supply_confirmation())
            conf[names[material]] = {"status": "Not Available", "ready_qty": 0, "eta": str(eta) if eta else ""}
            if state.transition(wfe.material_need_revision, reason, material, eta or "", note, conf,
                                flash="Need revision — rencana kembali ke PPC di H06."):
                st.rerun()


def _leader_panel(wf):
    if wf["status"] != wfe.MATERIAL_FINAL_CONFIRMED:
        st.caption("Approve tersedia setelah Supply Planner mengonfirmasi material final."
                   if wf["status"] == wfe.MATERIAL_FINAL_CONFIRMATION else "Tidak ada jadwal yang menunggu keputusan Leader.")
        return
    decision = st.radio("Keputusan", ["Approve Schedule", "Request Revision"], horizontal=True)
    if decision == "Approve Schedule":
        note = st.text_input("Catatan (opsional)")
        if st.button("✔ Approve Schedule", type="primary"):
            if state.transition(wfe.leader_approve, note, flash="APPROVED — PPC dapat merilis jadwal final."):
                st.rerun()
    else:
        reason = st.text_area("Alasan revisi (wajib)")
        if st.button("↩ Request Revision", type="primary"):
            if state.transition(wfe.leader_request_revision, reason, flash="Revisi diminta — kembali ke PPC (H06)."):
                st.rerun()


def _ppc_panel(wf, plan):
    s = wf["status"]
    if s == wfe.REVISION_REQUIRED:
        alert_row("Revision required", wf.get("revision_reason") or "-", RED, "refresh")
        st.button("Buka H06 What-if", type="primary", on_click=state.go_to, args=("What-if",))
    elif s == wfe.APPROVED:
        if st.button("🚀 Release Final Schedule", type="primary"):
            if state.transition(wfe.release_final, flash="READY FOR SAP RELEASE (simulasi)."):
                st.rerun()
    elif s == wfe.READY_FOR_SAP_RELEASE:
        sim_notice()
        if st.button("💾 Simpan & Eksekusi Jadwal (simulasi)", type="primary"):
            if state.transition(wfe.execute_schedule, flash="Jadwal dieksekusi (simulasi)."):
                st.rerun()
    elif s == wfe.RELEASED_SIMULATION:
        sim_notice("SCHEDULE_RELEASED_SIMULATION — eksekusi disimulasikan")
        if st.button("📥 Update Hasil Aktual", type="primary"):
            if do_update_actual():
                st.rerun()
    elif s in (wfe.MONITORING, wfe.REPLANNING_REQUIRED):
        st.success("Hasil aktual tercatat. Evaluasi di H08.")
        st.button("Buka H08 Performance", on_click=state.go_to, args=("Performance",))
    else:
        st.caption(f"Menunggu: {wfe.next_action(wf)[1]}")


def do_update_actual() -> bool:
    """Langkah 17: bangkitkan hasil aktual (terpisah dari rencana) lalu evaluasi pemicu."""
    wf = state.wf()
    data = state.get_data()
    res = state.result_for(wf["active_scenario"])
    actual = generate_actuals(wf["active_schedule"], data["bom"], wf["cycle"])
    hold_p1 = wf["active_kpi"]["stockout_risk"] - wf["active_kpi"]["delayed_orders"]
    akpi = actual_kpis(actual, res["buffer"], hold_p1, wf["revision_events"])
    trig = replanning_triggers(res, data, 1 if wf["supply_feedback"] else 0, akpi)
    return state.transition(wfe.update_actual, actual, akpi, replanning_needed(akpi, trig),
                            flash="Hasil aktual tercatat — evaluasi kinerja di H08.", goto="Performance")


def _audit(wf):
    section("Audit Trail")
    if not wf["history"]:
        st.caption("Belum ada aktivitas.")
        return
    h = pd.DataFrame(wf["history"])[::-1].copy()
    h["Waktu"] = h["Waktu"].str[11:]
    html_table(h, key_col="Aksi", sub_cols=("Catatan",), max_height=420)
    if wf["archive"]:
        with st.expander(f"Arsip rencana sebelumnya ({len(wf['archive'])})"):
            arc = pd.DataFrame([{"Dokumen": a["label"], "Skenario": a["scenario"], "Status akhir": a["status"],
                                 "Order": len(a["schedule"] or []), "Diarsipkan": a["archived_at"]} for a in wf["archive"]])
            html_table(arc, key_col="Dokumen")
