"""H08 — Performance Monitoring (PPC, Leader Produksi) · CFD langkah 18–19 + elemen mutu ISO 9001."""
import altair as alt
import pandas as pd
import streamlit as st

from engine import workflow_engine as wfe
from engine.actual_engine import plan_kpis_for_compare, plan_vs_actual
from engine.quality_engine import nonconformities, quality_objectives, replanning_triggers
from utils import state
from utils.helpers import fmt_dec, fmt_dt, fmt_int, fmt_rp
from utils.styles import (AMBER, GREEN, NAVY, ORANGE, RED, TEAL, W, banner, chart, html_table, kpi_card, section,
                          triggers_grid)


def _fmt(kind, v, sign=False):
    if kind == "pct":
        s = f"{abs(v) * 100:.1f}%" if sign else f"{v * 100:.1f}%"
        return (("+" if v > 0 else "−") + s) if sign and v else s
    if kind == "rp":
        s = fmt_rp(abs(v)) if sign else fmt_rp(v)
        return (("+" if v > 0 else "−") + s) if sign and v else s
    return (("+" if v > 0 else "") + fmt_int(v)) if sign else fmt_int(v)


def render():
    state.header("Performance", "18 Evaluasi kinerja: plan vs actual, sasaran mutu, ketidaksesuaian → 19 replanning.")
    ss = st.session_state
    wf = state.wf()
    data = state.get_data()
    actual = wf["actual"]
    res = state.result_for(wf["active_scenario"])
    plan_kpi = wf["active_kpi"] or res["kpi"]
    a = wf["actual_kpi"]

    if not actual:
        msg = {wfe.RELEASED_SIMULATION: "Jadwal sudah dieksekusi (simulasi) — tarik hasil aktual."}.get(
            wf["status"], f"Belum ada hasil aktual. Status: {wfe.label(wf['status'])}. KPI di bawah = rencana.")
        st.info(msg)
        if wf["status"] == wfe.RELEASED_SIMULATION and ss.role == state.PPC:
            from views.approval import do_update_actual
            if st.button("📥 Update Hasil Aktual", type="primary") and do_update_actual():
                st.rerun()
    k = a or plan_kpis_for_compare(plan_kpi, wf["revision_events"])
    tag = "aktual" if a else "rencana"

    cards = [("Stockout", k["stockout_risk"], f"SKU berisiko ({tag})", RED, "alert"),
             ("Overstock", k["overstock_risk"], "posisi > Max", AMBER, "box"),
             ("Delay", k["delayed_orders"], f"{fmt_dec(k['total_delay_days'], 1)} hari total", ORANGE, "clock"),
             ("Inventory Value", fmt_rp(k["inventory_value"]), "(NIP + produksi) × COGS", NAVY, "money"),
             ("Schedule Revision", wf["revision_events"], fmt_rp(wf["revision_events"] * 2_500_000), NAVY, "refresh"),
             ("Qty Achievement", f"{k['qty_achievement'] * 100:.1f}%", "aktual ÷ rencana", TEAL, "target"),
             ("Material Issue", k["material_issues"], "isu material saat eksekusi", RED if k["material_issues"] else TEAL, "truck"),
             ("Schedule Adherence", f"{k['schedule_adherence'] * 100:.0f}%", "selesai ≤ 4 jam dari plan", TEAL, "check")]
    for row in (cards[:4], cards[4:]):
        cols = st.columns(4)
        for col, (lbl, v, sub, color, ico) in zip(cols, row):
            with col:
                kpi_card(lbl, v, sub, color, ico)

    if actual:
        _plan_vs_actual(plan_kpi, a, actual, wf)
    _baseline_vs_approved(wf, plan_kpi)

    # Mutu ----------------------------------------------------------------------
    qo = quality_objectives({**plan_kpi, **k}, wf["revision_events"], len(state.get_data_issues()))
    left, right = st.columns([1.4, 1])
    with left:
        section("Sasaran Mutu", "ISO 9001 · 6.2 & 9.1")
        html_table(qo[["Sasaran", "Indikator", "Target", "Aktual", "Status"]], badges=("Status",), key_col="Sasaran",
                   sub_cols=("Indikator",))
    with right:
        section("Ketidaksesuaian", "ISO 9001 · 10.2")
        nc = nonconformities(res, actual)
        if nc.empty:
            st.success("Tidak ada ketidaksesuaian.")
        else:
            html_table(nc[["NC_ID", "SKU", "Kategori", "Temuan", "PIC"]], badges=("Kategori",), key_col="NC_ID",
                       sub_cols=("Temuan",), labels={"NC_ID": "No. NC"}, max_height=330)

    # Replanning --------------------------------------------------------------
    section("19 · Respon Perubahan & Replanning", "ISO 9001 · 6.3 pengendalian perubahan")
    trig = replanning_triggers(res, data, 1 if wf["supply_feedback"] else 0, a)
    fired = sum(t["on"] for t in trig)
    if fired:
        banner(f"REPLANNING REQUIRED — {fired} pemicu aktif", RED, "alert")
    else:
        banner("Kinerja sesuai rencana — tidak perlu replanning", GREEN, "check")
    triggers_grid(trig)
    if ss.role == state.PPC and wf["status"] in (wfe.MONITORING, wfe.REPLANNING_REQUIRED):
        st.write("")
        if st.button("↻ Start Replanning", type="primary"):
            reasons = ", ".join(t["name"] for t in trig if t["on"]) or "Evaluasi berkala"
            if state.transition(wfe.start_replanning, reasons, flash="Siklus baru dibuat — rencana lama diarsipkan.",
                                goto="What-if"):
                st.rerun()
    elif wf["archive"]:
        st.caption(f"{len(wf['archive'])} rencana sebelumnya tersimpan di arsip (lihat H07 Audit Trail).")


def _plan_vs_actual(plan_kpi, a, actual, wf):
    pva = plan_vs_actual(plan_kpis_for_compare(plan_kpi, wf["revision_events"]), a)
    left, right = st.columns([1, 1.25])
    with left:
        section("Plan vs Actual — KPI", wfe.revision_label(wf))
        t = pd.DataFrame([{"KPI": r.KPI, "Plan": _fmt(r.kind, r.Plan), "Actual": _fmt(r.kind, r.Actual),
                           "Deviasi": _fmt(r.kind, r.Deviasi, True), "Status": r.Status} for r in pva.itertuples()])
        html_table(t, badges=("Status",), key_col="KPI")
    with right:
        section("Plan vs Actual — Qty per SKU")
        df = pd.DataFrame(actual)
        long = df.melt(id_vars=["SKU_ID"], value_vars=["Planned_Qty", "Actual_Qty"], var_name="Jenis", value_name="Qty")
        long["Jenis"] = long["Jenis"].map({"Planned_Qty": "Plan", "Actual_Qty": "Actual"})
        ch = alt.Chart(long).mark_bar(cornerRadiusEnd=4, size=11).encode(
            y=alt.Y("SKU_ID:N", title=None, sort=list(df["SKU_ID"])), yOffset="Jenis:N",
            x=alt.X("Qty:Q", title=None, axis=alt.Axis(format="~s")),
            color=alt.Color("Jenis:N", scale=alt.Scale(domain=["Plan", "Actual"], range=["#A9B4C6", TEAL]), legend=alt.Legend(title=None)),
            tooltip=["SKU_ID", "Jenis", alt.Tooltip("Qty:Q", format=",.0f")])
        st.altair_chart(chart(ch, 300), **W)
    section("Plan vs Actual — Detail Order", "nilai rencana dan aktual disimpan terpisah")
    df = pd.DataFrame(actual)
    df["Status"] = df["Actual_Late"].map({True: "Late", False: "On time"})
    t = df[["SKU_ID", "Planned_Qty", "Actual_Qty", "Qty_Achievement", "Planned_Finish", "Actual_Finish",
            "Planned_ETA", "Actual_ETA", "Material_Variance", "Material_Issue", "Status"]]
    html_table(t, badges=("Status",), key_col="SKU_ID", sub_cols=("Material_Issue",),
               fmt={"Planned_Qty": fmt_int, "Actual_Qty": fmt_int, "Qty_Achievement": lambda v: f"{v * 100:.1f}%",
                    "Planned_Finish": fmt_dt, "Actual_Finish": fmt_dt, "Planned_ETA": fmt_dt, "Actual_ETA": fmt_dt,
                    "Material_Variance": lambda v: f"{v * 100:+.1f}%"},
               labels={"SKU_ID": "SKU", "Planned_Qty": "Qty plan", "Actual_Qty": "Qty aktual", "Qty_Achievement": "Achv.",
                       "Planned_Finish": "Selesai plan", "Actual_Finish": "Selesai aktual", "Planned_ETA": "ETA plan",
                       "Actual_ETA": "ETA aktual", "Material_Variance": "Scrap kemasan", "Material_Issue": "Isu"})


def _baseline_vs_approved(wf, plan_kpi):
    base = state.baseline_result()["kpi"]
    approved = wf["status"] in (wfe.APPROVED, wfe.READY_FOR_SAP_RELEASE, wfe.RELEASED_SIMULATION, wfe.MONITORING,
                                wfe.REPLANNING_REQUIRED)
    name = f"Approved ({wf['active_scenario']})" if approved else f"Rencana aktif ({wf['active_scenario']})"
    section("Baseline vs Approved Scenario")
    metrics = [("Stockout", "stockout_risk"), ("Overstock", "overstock_risk"), ("Delayed orders", "delayed_orders"),
               ("HOLD", "hold")]
    long = pd.DataFrame([{"KPI": l, "Skenario": n, "Nilai": d.get(k, 0)} for l, k in metrics
                         for n, d in (("Baseline", base), (name, plan_kpi))])
    c1, c2 = st.columns([1.4, 1])
    with c1:
        ch = alt.Chart(long).mark_bar(cornerRadiusEnd=4, size=12).encode(
            y=alt.Y("KPI:N", title=None, sort=[m[0] for m in metrics]), yOffset="Skenario:N",
            x=alt.X("Nilai:Q", title=None, axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("Skenario:N", scale=alt.Scale(range=["#A9B4C6", ORANGE]), legend=alt.Legend(title=None)),
            tooltip=["Skenario", "KPI", "Nilai"])
        st.altair_chart(chart(ch, 240), **W)
    with c2:
        rows = [{"KPI": l, "Baseline": fmt_int(base.get(k, 0)), name: fmt_int(plan_kpi.get(k, 0)),
                 "Δ": f"{plan_kpi.get(k, 0) - base.get(k, 0):+.0f}"} for l, k in metrics]
        rows.append({"KPI": "Inventory value", "Baseline": fmt_rp(base["inventory_value"]), name: fmt_rp(plan_kpi["inventory_value"]),
                     "Δ": fmt_rp(plan_kpi["inventory_value"] - base["inventory_value"])})
        html_table(pd.DataFrame(rows), key_col="KPI")
