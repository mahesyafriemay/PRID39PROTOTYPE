"""
Workflow Engine — state machine terpusat proses TO-BE DECIDE (CFD langkah 1–19).

Satu dict `wf` menjadi sumber kebenaran status siklus perencanaan. Setiap
transisi divalidasi (status asal + role) lalu dicatat di audit trail.
Semua fungsi murni (tanpa Streamlit) sehingga dapat diuji black-box.

    DRAFT ──(PPC mulai siklus + validasi data)──► FEASIBILITY_CHECK
    FEASIBILITY_CHECK ──(Supply: konfirmasi pasokan awal → gate)──► PLAN_REVIEW | HOLD
    PLAN_REVIEW ──(PPC accept)──────────────► MATERIAL_FINAL_CONFIRMATION
    PLAN_REVIEW | HOLD ──(PPC evaluate what-if / run)──► SCENARIO_REVIEW
    SCENARIO_REVIEW ──(PPC use selected scenario)──► MATERIAL_FINAL_CONFIRMATION
    MATERIAL_FINAL_CONFIRMATION ──(Supply confirm)──► MATERIAL_FINAL_CONFIRMED
                                └─(Supply need revision)──► REVISION_REQUIRED ─► H06
    MATERIAL_FINAL_CONFIRMED ──(Leader approve)──► APPROVED
                             └─(Leader request revision)──► REVISION_REQUIRED ─► H06
    APPROVED ──(PPC release)──► READY_FOR_SAP_RELEASE ──(simpan & eksekusi)──► RELEASED_SIMULATION
    RELEASED_SIMULATION ──(update aktual)──► MONITORING | REPLANNING_REQUIRED
    MONITORING | REPLANNING_REQUIRED ──(PPC start replanning)──► SCENARIO_REVIEW (siklus & revisi baru)
"""
from __future__ import annotations

import copy
from datetime import datetime

DRAFT = "DRAFT"
FEASIBILITY_CHECK = "FEASIBILITY_CHECK"
HOLD = "HOLD"
PLAN_REVIEW = "PLAN_REVIEW"
SCENARIO_REVIEW = "SCENARIO_REVIEW"
MATERIAL_FINAL_CONFIRMATION = "MATERIAL_FINAL_CONFIRMATION"
MATERIAL_FINAL_CONFIRMED = "MATERIAL_FINAL_CONFIRMED"
REVISION_REQUIRED = "REVISION_REQUIRED"
APPROVED = "APPROVED"
READY_FOR_SAP_RELEASE = "READY_FOR_SAP_RELEASE"
RELEASED_SIMULATION = "RELEASED_SIMULATION"
MONITORING = "MONITORING"
REPLANNING_REQUIRED = "REPLANNING_REQUIRED"

PPC, SUPPLY, LEADER = "PPC / Production Planner", "Supply Planner", "Leader Produksi"

STATUS_INFO = {
    # status: (label, langkah CFD, warna-kategori)
    DRAFT: ("Draft — siklus belum dimulai", "1–2", "neutral"),
    FEASIBILITY_CHECK: ("Feasibility check — menunggu konfirmasi pasokan awal", "6–8", "active"),
    HOLD: ("HOLD — tidak ada order layak, eskalasi ke What-if", "9", "bad"),
    PLAN_REVIEW: ("Plan review — PPC meninjau rencana", "10–11", "active"),
    SCENARIO_REVIEW: ("Scenario review — simulasi What-if", "12", "active"),
    MATERIAL_FINAL_CONFIRMATION: ("Menunggu konfirmasi material final (Supply Planner)", "13", "active"),
    MATERIAL_FINAL_CONFIRMED: ("Material final terkonfirmasi — menunggu Leader", "14", "active"),
    REVISION_REQUIRED: ("Revision required — kembali ke What-if", "12", "bad"),
    APPROVED: ("Approved — siap dirilis PPC", "15", "good"),
    READY_FOR_SAP_RELEASE: ("Ready for SAP release (simulasi)", "15–16", "good"),
    RELEASED_SIMULATION: ("Released (simulasi) — menunggu hasil aktual", "16–17", "good"),
    MONITORING: ("Monitoring — kinerja sesuai rencana", "18", "good"),
    REPLANNING_REQUIRED: ("Replanning required — PPC respon perubahan", "19", "bad"),
}

# Status sebelum rencana dikirim ke approval (rencana masih bisa diubah PPC)
EDITABLE = {PLAN_REVIEW, SCENARIO_REVIEW, HOLD, REVISION_REQUIRED}


class WorkflowError(Exception):
    pass


def new_workflow() -> dict:
    return {
        "status": DRAFT, "cycle": 1, "revision": 0, "revision_events": 0,
        "initial_supply": {}, "initial_supply_confirmed": False,
        "active_scenario": "Baseline", "active_params": None, "active_schedule": None, "active_kpi": None,
        "material_final": None, "leader_decision": None, "supply_feedback": None, "revision_reason": None,
        "execution_status": None, "actual": None, "actual_kpi": None,
        "history": [], "archive": [],
    }


def label(status: str) -> str:
    return STATUS_INFO[status][0]


def revision_label(wf: dict) -> str:
    return f"C{wf['cycle']:02d}-Rev.{wf['revision']:02d}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _require(wf, allowed, action):
    if wf["status"] not in allowed:
        raise WorkflowError(f"'{action}' tidak dapat dilakukan pada status {wf['status']}.")


def _role(role, allowed, action):
    if role not in allowed:
        raise WorkflowError(f"Role {role} tidak berwenang untuk '{action}'.")


def _log(wf, role, action, before, note="", scenario=None):
    wf["history"].append({
        "Waktu": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Siklus": wf["cycle"],
        "Revisi": f"Rev.{wf['revision']:02d}", "Role": role, "Aksi": action,
        "Skenario": scenario or wf["active_scenario"], "Status sebelum": before,
        "Status sesudah": wf["status"], "Catatan": note,
    })


def _move(wf, new_status, role, action, note="", scenario=None):
    before = wf["status"]
    wf["status"] = new_status
    _log(wf, role, action, before, note, scenario)
    return wf


# ---------------------------------------------------------------------------
# Langkah 1–3: mulai siklus & validasi data
# ---------------------------------------------------------------------------
def start_cycle(wf, data_issues: list, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Mulai siklus perencanaan")
    _require(wf, [DRAFT], "Mulai siklus perencanaan")
    if data_issues:
        _log(wf, role, "Validasi data gagal", wf["status"], "; ".join(data_issues))
        raise WorkflowError("Data tidak valid — perbaiki data sumber: " + "; ".join(data_issues))
    return _move(wf, FEASIBILITY_CHECK, role, "Mulai siklus & validasi data", "Data lengkap & valid")


# ---------------------------------------------------------------------------
# Langkah 7–9: konfirmasi pasokan awal → feasibility gate
# ---------------------------------------------------------------------------
def confirm_initial_supply(wf, confirmations: dict, gate_fn, role=SUPPLY):
    """gate_fn(confirmations) -> (n_layak, n_hold); dijalankan ulang setelah konfirmasi."""
    wf = copy.deepcopy(wf)
    _role(role, [SUPPLY], "Konfirmasi pasokan awal")
    _require(wf, [FEASIBILITY_CHECK, HOLD, PLAN_REVIEW, SCENARIO_REVIEW, REVISION_REQUIRED], "Konfirmasi pasokan awal")
    wf["initial_supply"] = copy.deepcopy(confirmations)
    wf["initial_supply_confirmed"] = True
    layak, hold = gate_fn(confirmations)
    summary = ", ".join(f"{k}: {v['status']}" for k, v in confirmations.items())
    if wf["status"] in (FEASIBILITY_CHECK, HOLD):
        new = HOLD if layak == 0 else PLAN_REVIEW
        return _move(wf, new, role, "Konfirmasi pasokan awal → feasibility gate",
                     f"{layak} LAYAK, {hold} HOLD · {summary}")
    _log(wf, role, "Update konfirmasi pasokan", wf["status"], f"{layak} LAYAK, {hold} HOLD · {summary}")
    return wf


# ---------------------------------------------------------------------------
# Langkah 11–12: review PPC & What-if
# ---------------------------------------------------------------------------
def evaluate_whatif(wf, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Evaluate with What-if")
    _require(wf, [PLAN_REVIEW, HOLD], "Evaluate with What-if")
    return _move(wf, SCENARIO_REVIEW, role, "Evaluate with What-if")


def run_scenario(wf, scenario_name: str, kpi: dict, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Run scenario")
    _require(wf, EDITABLE, "Run scenario")
    note = f"LAYAK {kpi['layak']} · HOLD {kpi['hold']} · late {kpi['delayed_orders']}"
    return _move(wf, SCENARIO_REVIEW, role, "Run scenario", note, scenario_name)


def _submit(wf, role, action, scenario_name, params, schedule_records, kpi):
    _role(role, [PPC], action)
    _require(wf, [PLAN_REVIEW, SCENARIO_REVIEW, REVISION_REQUIRED], action)
    if not schedule_records:
        raise WorkflowError("Tidak ada order LAYAK — rencana tidak dapat diajukan. Perbaiki di What-if.")
    wf.update(active_scenario=scenario_name, active_params=copy.deepcopy(params),
              active_schedule=copy.deepcopy(schedule_records), active_kpi=copy.deepcopy(kpi),
              material_final=None, leader_decision=None)
    return _move(wf, MATERIAL_FINAL_CONFIRMATION, role, action,
                 f"{len(schedule_records)} order · {revision_label(wf)}", scenario_name)


def accept_plan(wf, scenario_name, params, schedule_records, kpi, role=PPC):
    wf = copy.deepcopy(wf)
    if wf["status"] not in (PLAN_REVIEW, SCENARIO_REVIEW):
        raise WorkflowError(f"'Accept proposed plan' tidak dapat dilakukan pada status {wf['status']}.")
    return _submit(wf, role, "Accept proposed plan → konfirmasi material final", scenario_name, params,
                   schedule_records, kpi)


def use_scenario(wf, scenario_name, params, schedule_records, kpi, role=PPC):
    wf = copy.deepcopy(wf)
    return _submit(wf, role, "Use selected scenario → konfirmasi material final", scenario_name, params,
                   schedule_records, kpi)


# ---------------------------------------------------------------------------
# Langkah 13: Supply Planner konfirmasi material final
# ---------------------------------------------------------------------------
def confirm_final_material(wf, note="", role=SUPPLY):
    wf = copy.deepcopy(wf)
    _role(role, [SUPPLY], "Confirm final material")
    _require(wf, [MATERIAL_FINAL_CONFIRMATION], "Confirm final material")
    wf["material_final"] = {"decision": "Confirm", "note": note}
    return _move(wf, MATERIAL_FINAL_CONFIRMED, role, "Confirm final material", note)


def material_need_revision(wf, reason, material, updated_eta, note, new_supply: dict | None = None, role=SUPPLY):
    """new_supply: konfirmasi pasokan terbarui (ETA baru) → feasibility dihitung ulang di H06."""
    wf = copy.deepcopy(wf)
    _role(role, [SUPPLY], "Need revision (material)")
    _require(wf, [MATERIAL_FINAL_CONFIRMATION], "Need revision (material)")
    missing = [n for n, v in (("reason", reason), ("affected material", material), ("updated ETA", updated_eta),
                              ("note", note)) if not str(v or "").strip()]
    if missing:
        raise WorkflowError("Need Revision wajib mengisi: " + ", ".join(missing) + ".")
    fb = {"reason": reason, "material": material, "updated_eta": str(updated_eta), "note": note}
    wf.update(material_final={"decision": "Need Revision", **fb}, supply_feedback=fb, revision_reason=reason)
    if new_supply is not None:
        wf["initial_supply"], wf["initial_supply_confirmed"] = copy.deepcopy(new_supply), True
    wf["revision"] += 1
    wf["revision_events"] += 1
    return _move(wf, REVISION_REQUIRED, role, "Need revision (material)",
                 f"{reason} · {material} ETA {updated_eta} · {note}")


# ---------------------------------------------------------------------------
# Langkah 14: Leader Produksi
# ---------------------------------------------------------------------------
def leader_approve(wf, note="", role=LEADER):
    wf = copy.deepcopy(wf)
    _role(role, [LEADER], "Approve schedule")
    if wf["status"] != MATERIAL_FINAL_CONFIRMED:
        raise WorkflowError("Approve belum dapat dilakukan: material final belum dikonfirmasi Supply Planner.")
    wf["leader_decision"] = {"decision": "Approve", "note": note}
    return _move(wf, APPROVED, role, "Approve schedule", note)


def leader_request_revision(wf, reason, role=LEADER):
    wf = copy.deepcopy(wf)
    _role(role, [LEADER], "Request revision")
    _require(wf, [MATERIAL_FINAL_CONFIRMED], "Request revision")
    if not str(reason or "").strip():
        raise WorkflowError("Request Revision wajib mengisi alasan.")
    wf.update(leader_decision={"decision": "Request Revision", "reason": reason}, revision_reason=reason)
    wf["revision"] += 1
    wf["revision_events"] += 1
    return _move(wf, REVISION_REQUIRED, role, "Request revision", reason)


# ---------------------------------------------------------------------------
# Langkah 15–17: rilis, eksekusi (simulasi), hasil aktual
# ---------------------------------------------------------------------------
def release_final(wf, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Release final schedule")
    _require(wf, [APPROVED], "Release final schedule")
    return _move(wf, READY_FOR_SAP_RELEASE, role, "Release final schedule",
                 "SIMULATION ONLY — No real SAP transaction executed")


def execute_schedule(wf, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Simpan & eksekusi jadwal")
    _require(wf, [READY_FOR_SAP_RELEASE], "Simpan & eksekusi jadwal")
    wf["execution_status"] = "SCHEDULE_RELEASED_SIMULATION"
    return _move(wf, RELEASED_SIMULATION, role, "Simpan & eksekusi jadwal (simulasi)", "SCHEDULE_RELEASED_SIMULATION")


def update_actual(wf, actual_records: list, actual_kpi: dict, replanning_needed: bool, role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Update hasil aktual")
    _require(wf, [RELEASED_SIMULATION], "Update hasil aktual")
    wf.update(actual=copy.deepcopy(actual_records), actual_kpi=copy.deepcopy(actual_kpi),
              execution_status="EXECUTED_ACTUAL_RECORDED")
    new = REPLANNING_REQUIRED if replanning_needed else MONITORING
    return _move(wf, new, role, "Update hasil aktual → evaluasi kinerja",
                 f"Qty achievement {actual_kpi['qty_achievement']:.0%} · delay {actual_kpi['delayed_orders']}")


# ---------------------------------------------------------------------------
# Langkah 19: replanning (siklus baru, histori dipertahankan)
# ---------------------------------------------------------------------------
def start_replanning(wf, reason="Trigger replanning", role=PPC):
    wf = copy.deepcopy(wf)
    _role(role, [PPC], "Start replanning")
    _require(wf, [MONITORING, REPLANNING_REQUIRED], "Start replanning")
    wf["archive"].append({
        "cycle": wf["cycle"], "revision": wf["revision"], "label": revision_label(wf),
        "status": wf["status"], "scenario": wf["active_scenario"], "params": copy.deepcopy(wf["active_params"]),
        "schedule": copy.deepcopy(wf["active_schedule"]), "kpi": copy.deepcopy(wf["active_kpi"]),
        "actual": copy.deepcopy(wf["actual"]), "actual_kpi": copy.deepcopy(wf["actual_kpi"]),
        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    wf["cycle"] += 1
    wf["revision"] += 1
    wf.update(active_schedule=None, active_kpi=None, material_final=None, leader_decision=None,
              execution_status=None, actual=None, actual_kpi=None, supply_feedback=None)
    return _move(wf, SCENARIO_REVIEW, role, "Start replanning — siklus baru", reason)


# ---------------------------------------------------------------------------
# Panduan langkah berikutnya
# ---------------------------------------------------------------------------
def next_action(wf) -> tuple[str, str, str]:
    """(role, instruksi, halaman menu)."""
    s = wf["status"]
    return {
        DRAFT: (PPC, "Mulai siklus perencanaan & validasi data", "Overview"),
        FEASIBILITY_CHECK: (SUPPLY, "Konfirmasi pasokan awal di Feasibility Gate", "Feasibility"),
        HOLD: (PPC, "Eskalasi: perbaiki rencana di What-if", "What-if"),
        PLAN_REVIEW: (PPC, "Tinjau sequence & ETA, lalu Accept atau Evaluate with What-if", "Sequencing & ETA"),
        SCENARIO_REVIEW: (PPC, "Run scenario lalu Use Selected Scenario", "What-if"),
        MATERIAL_FINAL_CONFIRMATION: (SUPPLY, "Konfirmasi material final", "Decision Review"),
        MATERIAL_FINAL_CONFIRMED: (LEADER, "Approve atau request revision", "Decision Review"),
        REVISION_REQUIRED: (PPC, "Revisi skenario di What-if lalu ajukan ulang", "What-if"),
        APPROVED: (PPC, "Release final schedule", "Decision Review"),
        READY_FOR_SAP_RELEASE: (PPC, "Simpan & eksekusi jadwal (simulasi)", "Decision Review"),
        RELEASED_SIMULATION: (PPC, "Update hasil aktual", "Performance"),
        MONITORING: (PPC, "Pantau KPI; mulai replanning bila ada perubahan", "Performance"),
        REPLANNING_REQUIRED: (PPC, "Start replanning", "Performance"),
    }[s]
