"""H01 — Overview & Early Warning (PPC, Supply Planner, Leader Produksi)."""
import altair as alt
import pandas as pd
import streamlit as st

from engine import workflow_engine as wfe
from engine.quality_engine import quality_objectives
from utils import state
from utils.helpers import fmt_dt, fmt_int, fmt_rp
from utils.styles import (AMBER, NAVY, ORANGE, RED, STATUS_SCALE, TEAL, W, alert_row, chart, hero, html_table,
                          kpi_card, meter, section)


def render():
    res = state.active_result()
    kpi, buf, sch, mat, feas = res["kpi"], res["buffer"], res["schedule"], res["material"], res["feasibility"]
    wf = st.session_state.workflow
    issues = state.get_data_issues()
    qo = quality_objectives(kpi, wf["revision_events"], len(issues))

    hero("Production Decision Cockpit",
         f"Siklus C{wf['cycle']:02d} · {wfe.revision_label(wf)} · skenario {wf['active_scenario']} · "
         f"{'data valid' if not issues else f'{len(issues)} isu data'}",
         [("Qty direncanakan", fmt_int(kpi["planned_qty"])), ("Jam line", f"{kpi['line_hours']:.1f}"),
          ("Nilai inventori", fmt_rp(kpi["inventory_value"])),
          ("Sasaran mutu", f"{(qo['Status'] == 'Tercapai').sum()}/{len(qo)}")])
    _cycle_panel(wf, issues)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        kpi_card("Risk of Stockout", kpi["risk_skus"], f"NIP ≤ ROP · {kpi['watch_skus']} Watch", RED, "alert")
    with c2:
        kpi_card("Overstock", kpi["overstock_skus"], "NIP > Max Level", AMBER, "box")
    with c3:
        kpi_card("Potensi Delay", kpi["delayed_orders"] + kpi["hold"],
                 f"{kpi['delayed_orders']} ETA lewat · {kpi['hold']} HOLD", ORANGE, "clock")
    with c4:
        kpi_card("Schedule Revision", wf["revision_events"], f"Dokumen {wfe.revision_label(wf)}", NAVY, "refresh")

    # Early warning ------------------------------------------------------------
    left, right = st.columns([1, 2.1])
    with left:
        section("Status Buffer")
        cnt = buf["Status"].value_counts().reindex(["Risk", "Watch", "Normal"]).fillna(0).reset_index()
        cnt.columns = ["Status", "SKU"]
        donut = alt.Chart(cnt).mark_arc(innerRadius=62, cornerRadius=4, padAngle=0.02).encode(
            theta="SKU:Q", color=alt.Color("Status:N", scale=alt.Scale(**STATUS_SCALE), legend=alt.Legend(title=None)),
            tooltip=["Status", "SKU"])
        center = alt.Chart(pd.DataFrame({"t": [f"{len(buf)} SKU"]})).mark_text(
            fontSize=20, fontWeight=700, color=NAVY).encode(text="t:N")
        st.altair_chart(chart(donut + center, 250), **W)
    with right:
        section("Days of Cover vs Lead Time Produksi + QC", "batang < penanda = stok habis sebelum produksi selesai")
        d = buf.sort_values("Days_of_Cover")
        bars = alt.Chart(d).mark_bar(cornerRadiusEnd=5, size=14).encode(
            y=alt.Y("SKU_ID:N", sort=list(d["SKU_ID"]), title=None),
            x=alt.X("Days_of_Cover:Q", title="Hari"),
            color=alt.Color("Status:N", scale=alt.Scale(**STATUS_SCALE), legend=None),
            tooltip=["SKU_ID", "Nama_Produk", alt.Tooltip("Days_of_Cover:Q", format=".1f"), "Prod_QC_LT", "Status"])
        tick = alt.Chart(d).mark_tick(color=NAVY, thickness=2.5, size=20).encode(
            y=alt.Y("SKU_ID:N", sort=list(d["SKU_ID"])), x="Prod_QC_LT:Q")
        st.altair_chart(chart(bars + tick, 290), **W)

    # Material & jadwal ----------------------------------------------------------
    left, right = st.columns([1, 1.5])
    with left:
        section("Material Alert")
        hold = feas[feas["Status"] == "HOLD"] if not feas.empty else pd.DataFrame()
        for _, h in hold.iterrows():
            alert_row(f"HOLD · {h['SKU_ID']} ({h['Priority_Class']})", h["Reason"], RED, "stop")
        for _, m in mat[mat["Alert"] == "Supplier ETA Risk"].iterrows():
            eta = m["Supplier_ETA"].strftime("%d %b") if pd.notna(m["Supplier_ETA"]) else "-"
            alert_row(f"Supplier ETA Risk · {m['Material_Name']}",
                      f"{fmt_int(m['Incoming_Qty'])} {m['UoM']} · ETA {eta} · belum dikonfirmasi", AMBER, "truck")
        fb = wf["supply_feedback"]
        if fb:
            alert_row(f"Feedback Supply Planner · {fb['material']}", f"{fb['reason']} · ETA {fb['updated_eta']}", AMBER, "doc")
        ready = int((mat["Alert"] == "Material Ready").sum())
        meter("Material siap", ready, len(mat), TEAL, f"{ready}/{len(mat)} item")
    with right:
        section("Usulan Jadwal — Top 5")
        if sch.empty:
            st.info("Tidak ada order LAYAK pada skenario ini.")
        else:
            top = sch.head(5).copy()
            top["Status"] = top["Late"].map({True: "Late", False: "On time"})
            top = top[["Sequence", "SKU_ID", "Nama_Produk", "Priority_Class", "Quantity", "ETA_NDC", "Status"]]
            html_table(top, badges=("Priority_Class", "Status"), key_col="SKU_ID", sub_cols=("Nama_Produk",),
                       fmt={"Quantity": fmt_int, "ETA_NDC": fmt_dt},
                       labels={"Sequence": "#", "SKU_ID": "SKU", "Nama_Produk": "Produk", "Priority_Class": "Kelas",
                               "Quantity": "Qty", "ETA_NDC": "ETA NDC"})
    _data_sources()


def _data_sources():
    with st.expander("Sumber & kalibrasi data"):
        st.markdown(
            "- **Struktur & angka acuan** (SKU, stok, ROP, bobot AHP) mengikuti dokumen tugas TI4001 Kelompok 39.\n"
            "- **Konteks pasar**: Wardah merek BB cream terpopuler 2026 (Top Brand Index 29,3%) — "
            "[GoodStats](https://data.goodstats.id/statistic/top-5-brand-bb-cream-terpopuler-di-indonesia-2026-wardah-di-puncak-SpIqa).\n"
            "- **Harga jual** BB cream ±Rp30–39 ribu sejalan dengan harga ritel publik — "
            "[Female Daily](https://reviews.femaledaily.com/products/face-2/bb-cc-cream/wardah/lightening-bb-cream-light), "
            "[Medicastore](https://medicastore.com/produk/Kosmetik/27878/wardah-lightening-fresh-bb-tint-01-spf-32-neutral-fair-15-ml).\n"
            "- **Kalender campaign** 11.11 & 12.12 mengikuti kalender e-commerce nasional; Harbolnas 2026 berlangsung 10–16 Des — "
            "[Journal Arta](https://journalarta.com/news/2026/11/07/harbolnas-2026/).\n"
            "- Demand mingguan, BOM, material, supplier, dan kapasitas adalah **data simulasi** yang dikalibrasi ke angka di atas; "
            "tidak terhubung ke SAP maupun sistem perusahaan.")


def _cycle_panel(wf, issues):
    """Langkah 1–3 CFD: data diterima → PPC mulai siklus → validasi data."""
    c1, c2 = st.columns([2.2, 1])
    with c1:
        steps = [("1 · Data sumber diterima", "8 tabel", True),
                 ("2 · PPC mulai siklus", "Ya" if wf["status"] != wfe.DRAFT else "Belum", wf["status"] != wfe.DRAFT),
                 ("3 · Validasi data", "Valid" if not issues else f"{len(issues)} isu", not issues)]
        cols = st.columns(3)
        for col, (t, v, ok) in zip(cols, steps):
            col.metric(t, v)
        if issues:
            st.error("Data tidak valid — perencanaan tidak dapat dilanjutkan: " + " · ".join(issues))
    with c2:
        if wf["status"] == wfe.DRAFT:
            if st.session_state.role == state.PPC:
                if st.button("▶ Mulai Siklus Perencanaan", type="primary", **W):
                    if state.transition(wfe.start_cycle, issues, flash="Siklus dimulai — data valid. Lanjut Feasibility Gate.",
                                        goto="Feasibility"):
                        st.rerun()
            else:
                st.info("Menunggu PPC memulai siklus.")
        else:
            st.markdown(f"**Status siklus**  \n{wfe.label(wf['status'])}")
