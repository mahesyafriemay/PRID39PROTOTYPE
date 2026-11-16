"""H06 — What-if & Replanning (PPC; Leader Produksi sebagai reviewer) · CFD langkah 12 & 19."""
import altair as alt
import pandas as pd
import streamlit as st

from engine import workflow_engine as wfe
from engine.scenario_engine import default_scenario_params
from utils import state
from utils.helpers import fmt_dec, fmt_dt, fmt_int, fmt_rp
from utils.styles import ORANGE, RED, W, alert_row, chart, html_table, section

PRESETS = {
    "Custom": None,
    "Perbaikan HOLD (demo)": {"qty_override": {"BB-002": 72000}, "material_availability_pct": 110.0,
                              "supplier_eta_shift_days": -7, "line_capacity_pct": 120.0},
    "Supplier kirim 7 hari lebih cepat": {"supplier_eta_shift_days": -7},
    "Lonjakan demand +30%": {"demand_multiplier": 1.3},
    "Line turun 50% (breakdown)": {"line_capacity_pct": 50.0},
    "Material hanya 70% tersedia": {"material_availability_pct": 70.0},
}

KPI_ROWS = [("Stockout Risk", "stockout_risk", "int", True), ("Overstock", "overstock_risk", "int", True),
            ("HOLD SKU", "hold", "int", True), ("Layak SKU", "layak", "int", False),
            ("Delayed Orders", "delayed_orders", "int", True), ("Production Qty", "planned_qty", "int", None),
            ("Line Hours", "line_hours", "dec", None), ("Inventory Value", "inventory_value", "rp", True)]


def _fmt(kind, v, sign=False):
    if kind == "rp":
        s = fmt_rp(abs(v)) if sign else fmt_rp(v)
        return (("−" if v < 0 else "+") + s) if sign and v else s
    s = fmt_dec(v, 1) if kind == "dec" else fmt_int(v)
    return (("+" if v > 0 else "") + s) if sign else s


def render():
    state.header("What-if", "Ubah parameter → hitung ulang end-to-end (buffer → prioritas → gate → sequence → ETA) → bandingkan → pilih.")
    ss = st.session_state
    wf = state.wf()
    role = ss.role
    is_ppc = role == state.PPC
    editable = wf["status"] in wfe.EDITABLE
    base = state.baseline_result()

    if wf["status"] == wfe.REVISION_REQUIRED:
        fb = wf["supply_feedback"]
        if fb:
            alert_row(f"Revisi dari Supply Planner · {fb['material']}", f"{fb['reason']} · ETA baru {fb['updated_eta']} · {fb['note']}", RED, "truck")
        elif wf["leader_decision"]:
            alert_row("Revisi dari Leader Produksi", wf["leader_decision"].get("reason", ""), RED, "refresh")
    if not is_ppc:
        st.caption("👁 Leader Produksi: reviewer — melihat hasil skenario, keputusan oleh PPC.")
    elif not editable:
        st.info(f"Rencana sedang di tahap: {wfe.label(wf['status'])}. What-if aktif kembali saat revisi/replanning.")

    # Editor -----------------------------------------------------------------
    section("Scenario Editor")
    p = ss.params_alt
    preset = st.selectbox("Preset", list(PRESETS), disabled=not (is_ppc and editable))
    pv = {**default_scenario_params(), **PRESETS[preset]} if PRESETS[preset] else p
    lock = not (is_ppc and editable)
    with st.form("scenario_form"):
        c1, c2, c3 = st.columns(3)
        dm = c1.slider("Demand multiplier", 0.5, 2.0, float(pv["demand_multiplier"]), 0.05, disabled=lock)
        cu = c1.slider("Campaign uplift ×", 0.0, 2.0, float(pv["campaign_uplift_multiplier"]), 0.1, disabled=lock)
        ma = c2.slider("Material availability (%)", 0, 150, int(pv["material_availability_pct"]), 5, disabled=lock)
        se = c2.slider("Supplier ETA (± hari)", -14, 14, int(pv["supplier_eta_shift_days"]), disabled=lock)
        lc = c3.slider("Line capacity (%)", 10, 150, int(pv["line_capacity_pct"]), 5, disabled=lock)
        with c3.expander("Production quantity override"):
            elig = base["priority"][["SKU_ID", "Recommended_Qty"]].copy()
            elig["Qty_Scenario"] = elig["SKU_ID"].map(pv["qty_override"]).fillna(0).astype(int)
            edited = st.data_editor(
                elig, hide_index=True, key=f"qty_editor_{preset}", disabled=["SKU_ID", "Recommended_Qty"] + (["Qty_Scenario"] if lock else []),
                column_config={"Recommended_Qty": st.column_config.NumberColumn("Rekomendasi", format="%d"),
                               "Qty_Scenario": st.column_config.NumberColumn("Qty baru (0 = default)", min_value=0,
                                                                             step=1000, format="%d")})
        b1, b2, _ = st.columns([1, 1, 2])
        run = b1.form_submit_button("▶ Run Scenario", type="primary", disabled=lock, **W)
        reset = b2.form_submit_button("Reset", disabled=lock, **W)
    if run:
        override = {r.SKU_ID: int(r.Qty_Scenario) for r in edited.itertuples() if r.Qty_Scenario and r.Qty_Scenario > 0}
        ss.params_alt = {**default_scenario_params(), "demand_multiplier": dm, "campaign_uplift_multiplier": cu,
                         "material_availability_pct": float(ma), "supplier_eta_shift_days": int(se),
                         "line_capacity_pct": float(lc), "qty_override": override}
        ss.alt_ran = True
        if state.transition(wfe.run_scenario, "Alternative", state.result_for("Alternative")["kpi"],
                            flash="Skenario dihitung ulang end-to-end."):
            st.rerun()
    if reset:
        ss.params_alt = default_scenario_params()
        ss.alt_ran = False
        st.rerun()

    if not ss.alt_ran:
        st.info("Pilih preset atau atur parameter, lalu klik **Run Scenario**." if is_ppc else "Belum ada skenario alternatif.")
        _use_panel(base, None, is_ppc, editable)
        return
    alt_res = state.result_for("Alternative")

    # KPI comparison -----------------------------------------------------------
    left, right = st.columns([1.15, 1])
    with left:
        section("KPI Comparison", "Baseline vs Scenario")
        rows = []
        for lbl, k, kind, lower in KPI_ROWS:
            b, a = float(base["kpi"][k]), float(alt_res["kpi"][k])
            d = a - b
            verdict = "-" if lower is None or d == 0 else ("Lebih baik" if (d < 0) == lower else "Lebih buruk")
            rows.append({"KPI": lbl, "Baseline": _fmt(kind, b), "Scenario": _fmt(kind, a), "Delta": _fmt(kind, d, True),
                         "Dampak": verdict})
        html_table(pd.DataFrame(rows), badges=("Dampak",), key_col="KPI")
    with right:
        section("Visual Perubahan")
        ks = [("Stockout Risk", "stockout_risk"), ("HOLD SKU", "hold"), ("Layak SKU", "layak"), ("Delayed Orders", "delayed_orders")]
        long = pd.DataFrame([{"KPI": l, "Skenario": n, "Nilai": r["kpi"][k]} for l, k in ks
                             for n, r in (("Baseline", base), ("Scenario", alt_res))])
        ch = alt.Chart(long).mark_bar(cornerRadiusEnd=4, size=12).encode(
            y=alt.Y("KPI:N", title=None, sort=[l for l, _ in ks]), yOffset="Skenario:N",
            x=alt.X("Nilai:Q", title=None, axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("Skenario:N", scale=alt.Scale(range=["#A9B4C6", ORANGE]), legend=alt.Legend(title=None)),
            tooltip=["KPI", "Skenario", "Nilai"])
        st.altair_chart(chart(ch, 300), **W)

    # Schedule comparison ------------------------------------------------------
    section("Schedule Comparison")
    html_table(_schedule_compare(base, alt_res), badges=("B Class", "S Class", "B Feasibility", "S Feasibility"),
               key_col="SKU", fmt={"B Qty": fmt_int, "S Qty": fmt_int},
               labels={"B Qty": "Baseline Qty", "S Qty": "Scenario Qty", "B Class": "Baseline Class",
                       "S Class": "Scenario Class", "B Feasibility": "Baseline Feas.", "S Feasibility": "Scenario Feas.",
                       "B Seq": "Baseline Seq", "S Seq": "Scenario Seq", "B ETA": "Baseline ETA", "S ETA": "Scenario ETA"})
    _use_panel(base, alt_res, is_ppc, editable)


def _schedule_compare(base, alt_res) -> pd.DataFrame:
    def maps(r):
        b, p, f, s = r["buffer"], r["priority"], r["feasibility"], r["schedule"]
        return (dict(zip(b["SKU_ID"], b["Recommended_Qty"])),
                dict(zip(p["SKU_ID"], p["Priority_Class"])) if not p.empty else {},
                dict(zip(f["SKU_ID"], f["Status"])) if not f.empty else {},
                dict(zip(s["SKU_ID"], s["Sequence"])) if not s.empty else {},
                dict(zip(s["SKU_ID"], s["ETA_NDC"])) if not s.empty else {})
    bq, bc, bf, bs, be = maps(base)
    aq, ac, af, as_, ae = maps(alt_res)
    skus = [s for s in base["buffer"]["SKU_ID"] if bq.get(s, 0) or aq.get(s, 0)]
    return pd.DataFrame([{
        "SKU": s, "B Qty": bq.get(s, 0), "S Qty": aq.get(s, 0), "B Class": bc.get(s, "-"), "S Class": ac.get(s, "-"),
        "B Feasibility": bf.get(s, "-"), "S Feasibility": af.get(s, "-"),
        "B Seq": str(bs[s]) if s in bs else "-", "S Seq": str(as_[s]) if s in as_ else "-",
        "B ETA": fmt_dt(be[s]) if s in be else "-", "S ETA": fmt_dt(ae[s]) if s in ae else "-"} for s in skus])


def _use_panel(base, alt_res, is_ppc, editable):
    """Use Selected Scenario → simpan active_scenario/schedule/revision, lanjut ke konfirmasi material final (H07)."""
    wf = state.wf()
    section("Use Selected Scenario")
    opts = ["Baseline"] + (["Alternative"] if alt_res is not None else [])
    c1, c2 = st.columns([2, 1])
    choice = c1.radio("Skenario untuk proposed schedule", opts, horizontal=True,
                      index=opts.index(wf["active_scenario"]) if wf["active_scenario"] in opts else 0)
    if not is_ppc:
        c2.caption("Penetapan skenario oleh PPC.")
        return
    if c2.button("✔ Use Selected Scenario", type="primary", disabled=not editable or wf["status"] == wfe.HOLD and alt_res is None, **W):
        res = base if choice == "Baseline" else alt_res
        if state.transition(wfe.use_scenario, choice, state.scenario_params(choice), state.schedule_records(res), res["kpi"],
                            flash=f"Skenario {choice} dipakai — menunggu konfirmasi material final.", goto="Decision Review"):
            st.rerun()
    if wf["status"] == wfe.HOLD:
        st.caption("Status HOLD: jalankan skenario dulu (Run Scenario) untuk masuk scenario review.")
