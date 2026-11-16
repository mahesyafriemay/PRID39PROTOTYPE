"""
State management terpusat: role, akses halaman, workflow (engine/workflow_engine.py),
skenario aktif, dan akses hasil decision engine (cached).
"""
from __future__ import annotations

import copy
import json

import streamlit as st

from engine import workflow_engine as wfe
from engine.feasibility_engine import default_supply_confirmation
from engine.scenario_engine import default_scenario_params, run_pipeline
from utils.helpers import load_data, validate_data

ROLES = [wfe.PPC, wfe.SUPPLY, wfe.LEADER]
PPC, SUPPLY, LEADER = ROLES

# Menu → (kode, judul, role) — mengikuti UI Tree
PAGES = {
    "Overview": ("H01", "Overview & Early Warning", [PPC, SUPPLY, LEADER]),
    "Demand & Buffer": ("H02", "Demand & Buffer Monitoring", [PPC, LEADER]),
    "Priority": ("H03", "Production Priority Ranking", [PPC, LEADER]),
    "Feasibility": ("H04", "Feasibility Gate", [PPC, SUPPLY]),
    "Sequencing & ETA": ("H05", "Sequencing & ETA", [PPC, LEADER]),
    "What-if": ("H06", "What-if & Replanning", [PPC, LEADER]),
    "Decision Review": ("H07", "Decision Review & Approval", [PPC, SUPPLY, LEADER]),
    "Performance": ("H08", "Performance Monitoring", [PPC, LEADER]),
}
VIEW_ONLY = {("Demand & Buffer", LEADER), ("What-if", LEADER)}
DATA_LABEL = "Data simulasi · Tidak terhubung SAP"


def allowed_pages(role: str) -> list[str]:
    return [k for k, (_, _, roles) in PAGES.items() if role in roles]


def is_view_only(page: str, role: str) -> bool:
    return (page, role) in VIEW_ONLY


# ---------------------------------------------------------------------------
# Init & sinkronisasi
# ---------------------------------------------------------------------------
def init_state():
    ss = st.session_state
    ss.setdefault("role", PPC)
    ss.setdefault("nav", "Overview")
    ss.setdefault("params_baseline", default_scenario_params())
    ss.setdefault("params_alt", default_scenario_params())
    ss.setdefault("alt_ran", False)
    ss.setdefault("workflow", wfe.new_workflow())
    _sync()


def _sync():
    """Mirror variabel kunci workflow ke session_state (dibaca lintas halaman)."""
    wf = st.session_state.workflow
    st.session_state.active_scenario = wf["active_scenario"]
    st.session_state.active_schedule = wf["active_schedule"]
    st.session_state.active_revision = wfe.revision_label(wf)
    st.session_state.execution_status = wf["execution_status"]
    st.session_state.workflow_status = wf["status"]


def wf() -> dict:
    return st.session_state.workflow


def set_workflow(new: dict):
    st.session_state.workflow = new
    _sync()


def transition(fn, *args, flash: str | None = None, goto: str | None = None, **kwargs) -> bool:
    """Jalankan transisi workflow; tampilkan error bila ditolak state machine."""
    try:
        new = fn(wf(), *args, role=st.session_state.role, **kwargs)
    except wfe.WorkflowError as e:
        st.error(str(e))
        return False
    set_workflow(new)
    if flash:
        st.session_state.flash = flash
    if goto:
        st.session_state.nav = goto
    return True


def go_to(page: str):
    st.session_state.nav = page


# ---------------------------------------------------------------------------
# Data & hasil engine
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def get_data():
    return load_data()


@st.cache_data(show_spinner=False)
def get_data_issues():
    return validate_data(load_data())


@st.cache_data(show_spinner="Menghitung ulang decision engine...")
def _run_cached(params_json: str):
    return run_pipeline(load_data(), json.loads(params_json))


def supply_confirmation() -> dict:
    w = wf()
    return w["initial_supply"] if w["initial_supply_confirmed"] else default_supply_confirmation(get_data()["material"])


def full_params(params: dict, supply: dict | None = None) -> dict:
    return {**params, "supply_confirmation": supply if supply is not None else supply_confirmation()}


def run_with(params: dict) -> dict:
    return _run_cached(json.dumps(params, sort_keys=True, default=str))


def scenario_params(name: str) -> dict:
    return full_params(st.session_state.params_baseline if name == "Baseline" else st.session_state.params_alt)


def result_for(name: str) -> dict:
    return run_with(scenario_params(name))


def active_result() -> dict:
    return result_for(wf()["active_scenario"])


def baseline_result() -> dict:
    return result_for("Baseline")


def gate_fn(conf: dict):
    """Dipakai workflow saat Supply Planner konfirmasi pasokan awal."""
    k = run_with(full_params(scenario_params(wf()["active_scenario"]), conf))["kpi"]
    return k["layak"], k["hold"]


def schedule_records(res: dict) -> list:
    cols = ["Sequence", "SKU_ID", "Nama_Produk", "Kategori", "Priority_Class", "AHP_Score", "Critical_Ratio",
            "Quantity", "Production_Duration_Hours", "Start_Time", "Finish_Production", "Finish_QC", "ETA_NDC",
            "Target_Date", "Late", "Delay_Days", "Status_Delay"]
    s = res["schedule"]
    return [] if s.empty else copy.deepcopy(s[cols].to_dict("records"))


# ---------------------------------------------------------------------------
# Process tracker & header
# ---------------------------------------------------------------------------
def tracker_stages() -> list[dict]:
    res = active_result()
    k = res["kpi"]
    w = wf()
    s = w["status"]
    issues = get_data_issues()
    approval_val = {wfe.MATERIAL_FINAL_CONFIRMATION: "Konfirmasi material", wfe.MATERIAL_FINAL_CONFIRMED: "Review Leader",
                    wfe.APPROVED: "Approved", wfe.READY_FOR_SAP_RELEASE: "Ready release",
                    wfe.RELEASED_SIMULATION: "Released (sim)", wfe.REVISION_REQUIRED: "Revisi"}.get(s, "Belum diajukan")
    monitor_val = {wfe.MONITORING: "Sesuai rencana", wfe.REPLANNING_REQUIRED: "Replanning!"}.get(
        s, "Menunggu aktual" if s == wfe.RELEASED_SIMULATION else "—")
    return [
        {"code": "H01", "title": "Data & Siklus", "value": "Data valid" if not issues else f"{len(issues)} isu",
         "alert": bool(issues), "phase": "PLAN"},
        {"code": "H02", "title": "Buffer & ROP", "value": f"{k['risk_skus']} Risk", "alert": k["risk_skus"] > 0, "phase": "PLAN"},
        {"code": "H03", "title": "Prioritas", "value": f"{k['eligible']} SKU · {k['p1']} P1", "alert": k["p1"] > 0, "phase": "PLAN"},
        {"code": "H04", "title": "Feasibility", "value": f"{k['layak']} Layak · {k['hold']} Hold", "alert": k["hold"] > 0, "phase": "PLAN"},
        {"code": "H05", "title": "Sequence & ETA", "value": f"{k['layak']} order · {k['delayed_orders']} late",
         "alert": k["delayed_orders"] > 0, "phase": "PLAN"},
        {"code": "H06", "title": "What-if", "value": w["active_scenario"], "alert": s in (wfe.HOLD, wfe.REVISION_REQUIRED),
         "phase": "PLAN / ACT"},
        {"code": "H07", "title": "Approval & Rilis", "value": approval_val, "alert": s == wfe.REVISION_REQUIRED, "phase": "DO"},
        {"code": "H08", "title": "Monitoring", "value": monitor_val, "alert": s == wfe.REPLANNING_REQUIRED, "phase": "CHECK"},
    ]


def header(page: str, desc: str):
    from utils.styles import page_header

    code, title, roles = PAGES[page]
    users = ", ".join(r.split(" /")[0] for r in roles)
    w = wf()
    page_header(code, title, desc, users, rev=w["revision"], plan_status=w["status"].replace("_", " "),
                cycle=w["cycle"])
