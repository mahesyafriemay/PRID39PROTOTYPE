"""
Black-box test DECIDE sesuai CFD (TC01–TC19) + uji kalibrasi engine.
Jalankan dari root project:
    python -m pytest tests -v
atau tanpa pytest:
    python tests/test_engine.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import workflow_engine as wfe  # noqa: E402
from engine.actual_engine import actual_kpis, generate_actuals, plan_kpis_for_compare, plan_vs_actual  # noqa: E402
from engine.feasibility_engine import default_supply_confirmation  # noqa: E402
from engine.priority_engine import classify, criteria_weights_from_matrix  # noqa: E402
from engine.scenario_engine import run_pipeline  # noqa: E402
from utils.helpers import load_data, validate_data  # noqa: E402

DATA = load_data()
CONF = default_supply_confirmation(DATA["material"])
BASE = run_pipeline(DATA, {"supply_confirmation": CONF})
DEMO = {"qty_override": {"BB-002": 72000}, "material_availability_pct": 110.0, "supplier_eta_shift_days": -7,
        "line_capacity_pct": 120.0, "supply_confirmation": CONF}
PPC, SUP, LEAD = wfe.PPC, wfe.SUPPLY, wfe.LEADER


def records(res):
    return res["schedule"].to_dict("records")


def gate(conf):
    k = run_pipeline(DATA, {"supply_confirmation": conf})["kpi"]
    return k["layak"], k["hold"]


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except wfe.WorkflowError:
        return True
    return False


def wf_at_plan_review():
    wf = wfe.start_cycle(wfe.new_workflow(), [], role=PPC)
    return wfe.confirm_initial_supply(wf, CONF, gate, role=SUP)


def wf_at_material_final():
    return wfe.accept_plan(wf_at_plan_review(), "Baseline", {}, records(BASE), BASE["kpi"], role=PPC)


def wf_released():
    wf = wfe.confirm_final_material(wf_at_material_final(), role=SUP)
    wf = wfe.leader_approve(wf, role=LEAD)
    wf = wfe.release_final(wf, role=PPC)
    return wfe.execute_schedule(wf, role=PPC)


# ---------------------------------------------------------------------------
# Kalibrasi engine terhadap dokumen
# ---------------------------------------------------------------------------
def test_buffer_matches_document():
    b = BASE["buffer"].set_index("SKU_ID")
    assert abs(b.loc["BB-001", "ROP"] - 59380) < 50
    assert abs(b.loc["BB-002", "Days_of_Cover"] - 16.6) < 0.1
    assert b.loc["BB-002", "Recommended_Qty"] == 85000


def test_ahp_weights_consistent():
    res = criteria_weights_from_matrix()
    assert res["CR"] < 0.1
    assert [round(x, 3) for x in res["weights"]] == [0.519, 0.201, 0.201, 0.079]


# ---------------------------------------------------------------------------
# TC01–TC19
# ---------------------------------------------------------------------------
def test_TC01_valid_data_continues_to_buffer():
    assert validate_data(DATA) == []
    wf = wfe.start_cycle(wfe.new_workflow(), validate_data(DATA), role=PPC)
    assert wf["status"] == wfe.FEASIBILITY_CHECK
    assert len(BASE["buffer"]) == 12 and BASE["buffer"]["ROP"].notna().all()


def test_TC02_data_issue_blocks_planning():
    bad = {k: v.copy() for k, v in DATA.items()}
    bad["sku_master"] = bad["sku_master"].drop(columns=["Lifecycle"])
    issues = validate_data(bad)
    assert issues
    assert raises(wfe.start_cycle, wfe.new_workflow(), issues, role=PPC)


def test_TC03_nip_above_rop_not_recommended():
    b = BASE["buffer"]
    above = b[b["NIP"] > b["ROP"]]
    assert len(above) and (above["Recommended_Qty"] == 0).all()
    assert not set(above["SKU_ID"]) & set(BASE["priority"]["SKU_ID"])


def test_TC04_nip_below_rop_has_qty():
    b = BASE["buffer"]
    below = b[b["NIP"] <= b["ROP"]]
    assert len(below) == 8 and (below["Recommended_Qty"] > 0).all()
    assert (below["Recommended_Qty"] % below["Batch_Size"] == 0).all()


def test_TC05_material_shortage_hold():
    f = BASE["feasibility"].set_index("SKU_ID")
    assert f.loc["BB-002", "Status"] == "HOLD"
    assert f.loc["BB-002", "Reason"].startswith("Material shortage") and "12.000" in f.loc["BB-002", "Reason"]


def test_TC06_capacity_unavailable_hold():
    r = run_pipeline(DATA, {"supply_confirmation": CONF, "line_capacity_pct": 20.0})
    cap = r["feasibility"][~r["feasibility"]["Capacity_OK"]]
    assert len(cap) and (cap["Status"] == "HOLD").all()
    assert cap["Reason"].str.contains("Capacity unavailable").all()


def test_TC07_hold_not_in_sequence():
    hold = set(BASE["feasibility"].query("Status == 'HOLD'")["SKU_ID"])
    assert hold and not hold & set(BASE["sequence"]["SKU_ID"])


def test_TC08_layak_in_sequence():
    layak = set(BASE["feasibility"].query("Status == 'LAYAK'")["SKU_ID"])
    assert layak == set(BASE["sequence"]["SKU_ID"])


def test_TC09_p1_first():
    ranks = BASE["sequence"]["Priority_Class"].map({"P1": 1, "P2": 2, "P3": 3}).tolist()
    assert ranks == sorted(ranks) and ranks[0] == 1
    assert classify(pd.Series([0.5, 1.2, 1.5, 2.0]), pd.Series([0.1, 0.4, 0.3, 0.2]))[0] == ["P1", "P2", "P2", "P3"]


def test_TC10_whatif_changes_result():
    alt = run_pipeline(DATA, DEMO)
    assert alt["kpi"]["hold"] < BASE["kpi"]["hold"]
    assert list(alt["schedule"]["SKU_ID"]) != list(BASE["schedule"]["SKU_ID"])
    wf = wfe.run_scenario(wfe.evaluate_whatif(wf_at_plan_review(), role=PPC), "Alternative", alt["kpi"], role=PPC)
    assert wf["status"] == wfe.SCENARIO_REVIEW
    assert raises(wfe.run_scenario, wf, "Alternative", alt["kpi"], role=LEAD)        # Leader hanya reviewer


def test_TC11_use_selected_scenario_feeds_h07():
    alt = run_pipeline(DATA, DEMO)
    wf = wfe.run_scenario(wf_at_plan_review(), "Alternative", alt["kpi"], role=PPC)
    wf = wfe.use_scenario(wf, "Alternative", DEMO, records(alt), alt["kpi"], role=PPC)
    assert wf["status"] == wfe.MATERIAL_FINAL_CONFIRMATION and wf["active_scenario"] == "Alternative"
    assert [r["SKU_ID"] for r in wf["active_schedule"]] == list(alt["schedule"]["SKU_ID"])


def test_TC12_leader_cannot_approve_before_final_material():
    wf = wf_at_material_final()
    assert raises(wfe.leader_approve, wf, role=LEAD)
    assert raises(wfe.confirm_final_material, wf, role=LEAD)                       # role lain ditolak


def test_TC13_supply_need_revision_back_to_h06():
    wf = wf_at_material_final()
    assert raises(wfe.material_need_revision, wf, "Tube telat", "Tube & tutup 20 ml", "", "kirim parsial", role=SUP)
    wf = wfe.material_need_revision(wf, "Tube telat", "Tube & tutup 20 ml", "2026-11-25", "kirim parsial", role=SUP)
    assert wf["status"] == wfe.REVISION_REQUIRED and wfe.next_action(wf)[2] == "What-if"


def test_TC14_leader_request_revision_increments_revision():
    wf = wfe.confirm_final_material(wf_at_material_final(), role=SUP)
    rev = wf["revision"]
    assert raises(wfe.leader_request_revision, wf, "  ", role=LEAD)
    wf = wfe.leader_request_revision(wf, "Shift Rabu terbatas", role=LEAD)
    assert wf["status"] == wfe.REVISION_REQUIRED and wf["revision"] == rev + 1
    assert wfe.next_action(wf)[2] == "What-if"


def test_TC15_leader_approve_enables_release():
    wf = wfe.leader_approve(wfe.confirm_final_material(wf_at_material_final(), role=SUP), role=LEAD)
    assert wf["status"] == wfe.APPROVED
    assert raises(wfe.release_final, wf, role=LEAD)
    assert wfe.release_final(wf, role=PPC)["status"] == wfe.READY_FOR_SAP_RELEASE


def test_TC16_release_final_schedule_simulation():
    wf = wf_released()
    assert wf["status"] == wfe.RELEASED_SIMULATION
    assert wf["execution_status"] == "SCHEDULE_RELEASED_SIMULATION"


def test_TC17_actual_stored_separately():
    wf = wf_released()
    planned_before = [dict(r) for r in wf["active_schedule"]]
    act = generate_actuals(wf["active_schedule"], DATA["bom"], wf["cycle"])
    k = actual_kpis(act, BASE["buffer"], 1, wf["revision_events"])
    wf = wfe.update_actual(wf, act, k, True, role=PPC)
    assert wf["actual"] is not None and wf["active_schedule"] == planned_before
    assert all({"Planned_Qty", "Actual_Qty", "Planned_ETA", "Actual_ETA"} <= r.keys() for r in wf["actual"])
    assert any(r["Planned_Qty"] != r["Actual_Qty"] for r in wf["actual"])


def test_TC18_plan_vs_actual_deviation():
    act = generate_actuals(records(BASE), DATA["bom"], 1)
    k = actual_kpis(act, BASE["buffer"], 1, 0)
    pva = plan_vs_actual(plan_kpis_for_compare(BASE["kpi"], 0), k)
    assert {"Plan", "Actual", "Deviasi"} <= set(pva.columns) and len(pva) == 8
    assert pva.set_index("key").loc["qty_achievement", "Deviasi"] < 0


def test_TC19_replanning_new_cycle_keeps_history():
    wf = wf_released()
    act = generate_actuals(wf["active_schedule"], DATA["bom"], wf["cycle"])
    wf = wfe.update_actual(wf, act, actual_kpis(act, BASE["buffer"], 1, 0), True, role=PPC)
    assert wf["status"] == wfe.REPLANNING_REQUIRED
    n_hist, rev = len(wf["history"]), wf["revision"]
    wf2 = wfe.start_replanning(wf, "Late order", role=PPC)
    assert wf2["cycle"] == 2 and wf2["revision"] == rev + 1 and wf2["status"] == wfe.SCENARIO_REVIEW
    assert len(wf2["archive"]) == 1 and wf2["archive"][0]["schedule"] == wf["active_schedule"]
    assert len(wf2["history"]) == n_hist + 1 and wf2["history"][:n_hist] == wf["history"]
    h = wf2["history"][-1]
    assert {"Waktu", "Siklus", "Revisi", "Role", "Aksi", "Skenario", "Status sebelum", "Status sesudah", "Catatan"} <= h.keys()


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{passed}/{len(tests)} test lulus")
    sys.exit(0 if passed == len(tests) else 1)
