"""虚构网络验收断言（只读）。

对应题目验收剧本：
P1 双药店同日同成分重叠 + 停诊 + 同一给药期；规则与图模型同链；
P2/P3 弱身份不合并、不扩展；P4 调拨解释合法供药；P5 申诉退回关闭。
另含病例组合基准三项：未标准化均值差、共同支持失败不外推、小组病例区间。
"""
import math
from datetime import date

from sqlalchemy.orm import Session

from .models import (
    Appeal, Case, CaseJudgment, GraphEdge, IdentityEvidence, SourceRecord,
)
from .services import casemix as cm
from .services import identity
from .services.expansion import expand, high_amount_extension
from .services.ranking import rank_cases


def _chk(key, name, passed, expected, actual, detail=""):
    return {"key": key, "name": name, "passed": bool(passed),
            "expected": expected, "actual": actual, "detail": detail}


def run_acceptance(db: Session) -> dict:
    checks = []

    # 1) 来源账 ----------------------------------------------------------
    src_total = db.query(SourceRecord).count()
    no_citation = db.query(SourceRecord).filter(
        (SourceRecord.citation.is_(None)) | (SourceRecord.citation == "")).count()
    old = db.query(SourceRecord).filter_by(code="SRC-ST5B").first()
    corr = db.query(SourceRecord).filter_by(code="SRC-ST5B-C2").first()
    checks.append(_chk(
        "source_ledger", "来源账：记录可引用且更正链完整",
        src_total >= 50 and no_citation == 0 and old and old.status == "corrected"
        and corr and corr.supersedes_id == old.id,
        ">=50 条来源、0 条缺引用、SRC-ST5B 被更正单接续",
        {"total": src_total, "missing_citation": no_citation,
         "old_status": old.status if old else None,
         "correction_linked": bool(corr and old and corr.supersedes_id == old.id)}))

    # 2) 身份解析分级 -----------------------------------------------------
    ie01 = db.query(IdentityEvidence).filter_by(code="IE-01").first()
    ie03 = db.query(IdentityEvidence).filter_by(code="IE-03").first()
    v, p2c, c2m = identity.cluster_map(db)
    p1_same = p2c.get("P1") == p2c.get("P1B")
    p23_sep = p2c.get("P2") != p2c.get("P3")
    checks.append(_chk(
        "identity_grading", "强证据成簇、弱电话候选不合并",
        ie01 and ie01.tier == "strong" and ie01.status == "auto" and p1_same
        and ie03 and ie03.tier == "weak" and ie03.status == "pending" and p23_sep,
        "P1/P1B 同强簇；P2/P3 异簇且 IE-03 pending",
        {"identity_version": v.version if v else None,
         "P1_cluster": p2c.get("P1"), "P1B_cluster": p2c.get("P1B"),
         "P2_cluster": p2c.get("P2"), "P3_cluster": p2c.get("P3"),
         "IE-03_status": ie03.status if ie03 else None}))

    # 3) P1 必要事实 ------------------------------------------------------
    c1 = db.query(Case).filter_by(code="C-001").first()
    j1 = db.query(CaseJudgment).filter_by(id=c1.current_judgment_id).first()
    rr1 = j1.rule_result
    facts = {f["key"]: f for f in rr1["necessary_facts"]}
    chain1 = rr1["support_chain"]
    required_uids = [
        "edge:identity_strong:IE-01",
        "edge:rx_settlement:RX1__ST1", "edge:rx_settlement:RX2__ST2",
        "edge:settle_org:ST1__PH1", "edge:settle_org:ST2__PH2",
        "edge:ingredient_overlap:ST1__ST2",
        "edge:schedule_conflict:RX1__SCH1",
    ]
    missing = [u for u in required_uids if u not in chain1]
    p1_facts_ok = (
        rr1["fired"]
        and facts["distinct_pharmacies"]["passed"]
        and facts["same_day"]["passed"]
        and facts["qty_overlap"]["value"] >= 28
        and facts["same_admin_period"]["passed"]
        and facts["schedule_conflict"]["passed"]
        and not missing
    )
    checks.append(_chk(
        "p1_chain", "P1：两药店同日同成分数量重叠+停诊+同一给药期，支持链完整",
        p1_facts_ok, "全部必要事实成立且 7 条核心边在链",
        {"fired": rr1["fired"],
         "overlap_qty": facts["qty_overlap"]["value"],
         "same_admin_period": facts["same_admin_period"]["passed"],
         "schedule": facts["schedule_conflict"]["passed"],
         "missing_uids": missing, "chain": chain1}))

    # 4) 规则与图模型同链 + 同目标排序 P1 居首 ------------------------------
    mr1 = j1.model_result or {}
    same_chain = mr1.get("trace_chain_uids") == chain1
    open_rows = [r for r in rank_cases(db, "MULTI_POINT", "model")
                 if r["status"] in ("pending", "reviewing", "new")]
    rule_rows = [r for r in rank_cases(db, "MULTI_POINT", "amount_completeness")
                 if r["status"] in ("pending", "reviewing", "new")]
    top_model = open_rows[0]["code"] if open_rows else None
    top_rule = rule_rows[0]["code"] if rule_rows else None
    checks.append(_chk(
        "same_chain_and_rank", "规则与图模型在 P1 上展示相同支持链，且同目标排序居首",
        same_chain and top_model == "C-001" and top_rule == "C-001"
        and (mr1.get("calibrated_score") or 0) >= 55,
        "trace_chain_uids == support_chain；两种排序开放线索第一均为 C-001；模型分≥55",
        {"same_chain": same_chain, "top_model": top_model, "top_rule": top_rule,
         "model_score": mr1.get("calibrated_score"),
         "rule_score": j1.rule_result.get("raw_score")}))

    # 5) P2/P3 弱身份：只给待核边，不扩展 --------------------------------
    exp23 = expand(db, "beneficiary", "P2")
    uids23 = {e["uid"] for e in exp23["edges"]}
    nodes23 = {n["uid"] for n in exp23["nodes"]}
    weak_ok = ("edge:identity_weak:IE-03" in uids23
               and "person:P3" in exp23["terminal_candidates"]
               and "settlement:ST4" not in nodes23
               and "rx:RX4" not in nodes23)
    checks.append(_chk(
        "p23_weak_no_expand", "P2/P3：弱电话候选只生成待核边，不沿 P3 继续扩展",
        weak_ok, "IE-03 在图；P3 为 terminal；ST4/RX4 不可达",
        {"weak_edge_present": "edge:identity_weak:IE-03" in uids23,
         "terminal": exp23["terminal_candidates"],
         "ST4_reached": "settlement:ST4" in nodes23,
         "RX4_reached": "rx:RX4" in nodes23}))

    # 6) P4 调拨解释 + 否定边不扩展 --------------------------------------
    c3 = db.query(Case).filter_by(code="C-003").first()
    j3 = db.query(CaseJudgment).filter_by(id=c3.current_judgment_id).first()
    inv = next((c for c in j3.counter_result.get("checks", [])
                if c["check_type"] == "inventory"), None)
    ov = db.query(GraphEdge).filter(
        GraphEdge.edge_type == "ingredient_overlap",
        GraphEdge.uid.like("%ST5A%ST5B%")).first()
    exp4 = expand(db, "claim", "ST5A")
    exp4_uids = {e["uid"] for e in exp4["edges"]}
    p4_ok = (c3.status == "excluded_legitimate" and inv
             and inv["status"] == "checked_explains" and "TR4" in inv["detail"]
             and ov and ov.status == "negated"
             and "edge:ingredient_overlap:ST5A__ST5B" not in exp4_uids)
    checks.append(_chk(
        "p4_transfer", "P4：调拨+红冲解释表面重复，重叠边否定且不再扩展",
        p4_ok, "C-003 excluded_legitimate；inventory=checked_explains(TR4)；overlap negated 且扩展不可见",
        {"status": c3.status, "inventory": inv and inv["status"],
         "overlap_status": ov and ov.status,
         "overlap_in_expansion": "edge:ingredient_overlap:ST5A__ST5B" in exp4_uids}))

    # 7) P5 申诉退回 -----------------------------------------------------
    app1 = db.query(Appeal).filter_by(code="APP1").first()
    c4 = db.query(Case).filter_by(code="C-004").first()
    j4 = db.query(CaseJudgment).filter_by(id=c4.current_judgment_id).first()
    ap = next((c for c in j4.counter_result.get("checks", [])
               if c["check_type"] == "appeal"), None)
    checks.append(_chk(
        "p5_appeal_return", "P5：申诉成立且确认药品退回，线索关闭",
        app1 and app1.result == "upheld" and app1.drug_returned
        and c4.status == "closed_returned" and ap and ap["status"] == "confirmed"
        and ap["effect"] == "close",
        "APP1 upheld/returned；C-004 closed_returned；appeal check confirmed/close",
        {"appeal": app1.result if app1 else None,
         "returned": app1.drug_returned if app1 else None,
         "case_status": c4.status, "check": ap and ap["status"]}))

    # 8) 默认受限扩展 ----------------------------------------------------
    exp1 = expand(db, "beneficiary", "P1")
    cutoff = date(2026, 3, 31)
    bad_dates = [e["uid"] for e in exp1["edges"] if e["event_date"]
                 and not (date.fromisoformat("2025-10-03")
                          <= date.fromisoformat(e["event_date"]) <= cutoff)]
    n1 = {n["uid"] for n in exp1["nodes"]}
    bounds_ok = (exp1["params"]["max_hops"] == 2 and exp1["params"]["window_days"] == 180
                 and not bad_dates and "schedule:SCH1" in n1
                 and "org:PH1" in n1 and "org:PH2" in n1)
    checks.append(_chk(
        "restricted_expansion", "默认扩展：同强簇、两跳、180 日，主链节点可达且无越窗边",
        bounds_ok, "hops=2/window=180；SCH1/PH1/PH2 可达；无越窗边",
        {"node_count": len(exp1["nodes"]), "edge_count": len(exp1["edges"]),
         "out_of_window": bad_dates}))

    # 9) 高金额跨机构扩展门槛 --------------------------------------------
    low = high_amount_extension(db, "C-001", freeze=False)
    high = high_amount_extension(db, "C-007", freeze=False)
    bed_reached = any(n["node_type"] == "bed" for n in high.get("nodes", []))
    checks.append(_chk(
        "high_amount_gate", "高金额跨机构扩展须模式显式授权：P1 被拒、C-007 授权并加入床位边",
        low["authorized"] is False and high["authorized"] is True and bed_reached,
        "C-001 金额不足拒绝；C-007 授权且 bed 节点可达",
        {"c001": low.get("reason"), "c007_authorized": high.get("authorized"),
         "added": high.get("extension", {}).get("added_edge_types"),
         "bed_reached": bed_reached}))

    # 10) 快照与不可变判断 ------------------------------------------------
    from .models import GraphSnapshot
    snap1 = db.query(GraphSnapshot).filter_by(snapshot_no=1).first()
    snap_ok = (snap1 and snap1.cutoff_date.isoformat() == "2026-03-31"
               and snap1.identity_version >= 1 and snap1.policy_versions
               and snap1.graph.get("nodes") and j1.version == 1
               and j1.snapshot_no == 1 and j1.disclaimer)
    checks.append(_chk(
        "snapshot_immutable", "快照 #1 冻结四要素，首份判断不可变且带免责声明",
        snap_ok, "cutoff/identity_version/edge_scope/policy_versions 齐全；judgment v1@snap1",
        {"cutoff": snap1.cutoff_date.isoformat() if snap1 else None,
         "identity_version": snap1.identity_version if snap1 else None,
         "policies": len(snap1.policy_versions) if snap1 else 0,
         "judgment_version": j1.version, "snapshot_no": j1.snapshot_no}))

    # ============================================================
    # 病例组合基准（case-mix）三项核验
    # ============================================================
    # 11) 未经标准化的总均值差：病例结构差异被误读为费用异常 ----------------
    unstd = cm.unstandardized(db)
    raw = unstd["core"]["by_org"]
    h6_raw, h7_raw = raw.get("H6"), raw.get("H7")
    raw_diff = unstd["core"]["mean_diff"]
    direct = cm.direct_standardization(db)
    d_rows = {r["org_code"]: r for r in direct["orgs"]}
    d6, d7 = d_rows.get("H6"), d_rows.get("H7")
    # 标准化后：同组参考均值完全一致 → 残差≈0、观察值落区间内
    std_ok = (
        h6_raw and h7_raw
        and h6_raw["episodes"] == 100 and h7_raw["episodes"] == 100
        and h6_raw["mean_amount"] == 6000.0 and h7_raw["mean_amount"] == 4000.0
        and raw_diff == 2000.0
        and d6 and d7
        and abs(d6["residual"]["total_amount"]) < 1.0
        and abs(d7["residual"]["total_amount"]) < 1.0
        and d6["residual"]["within_interval"] and d7["residual"]["within_interval"]
    )
    checks.append(_chk(
        "casemix_raw_vs_standardized",
        "病例组合：未标准化人均差 2000 元（重症60%vs20%），标准化后残差≈0",
        std_ok,
        "原始人均 H6=6000/H7=4000（差2000）；直接标准化两机构残差均≈0且在期望区间内",
        {"raw_H6": h6_raw and h6_raw["mean_amount"],
         "raw_H7": h7_raw and h7_raw["mean_amount"], "raw_diff": raw_diff,
         "std_residual_H6": d6 and d6["residual"]["total_amount"],
         "std_residual_H7": d7 and d7["residual"]["total_amount"],
         "expected_H6": d6 and d6["expected"]["total_amount"],
         "observed_H6": d6 and d6["observed"]["total_amount"]}))

    # 12) 共同支持失败：未覆盖/单机构病例禁止外推 --------------------------
    layers = cm.stratify(db)
    fail_groups = {f["group_code"] for f in layers["common_support_failures"]}
    uncovered_codes = {u["episode"] for u in layers["uncovered"]}
    gates = cm.gate_decisions(db)
    gated_episodes = {c for g in gates for c in g["episodes"]}
    # 直接标准化：罕见组不得出现在 supported_groups
    # 匹配路线：同样必须拒绝外推
    matching = cm.matching_standardization(db)
    m_excluded = {x["code"] for r in matching["orgs"] for x in r["excluded_episodes"]}
    rare_eps = {f"EP-H6-{101 + i:03d}" for i in range(4)}
    support_ok = (
        "CG-RARE" in fail_groups
        and "EP-H7-101" in uncovered_codes
        and rare_eps <= gated_episodes and "EP-H7-101" in gated_episodes
        and "CG-RARE" not in direct["supported_groups"]
        and all(x in gated_episodes for x in rare_eps | {"EP-H7-101"})
        and rare_eps <= m_excluded and "EP-H7-101" in m_excluded
    )
    checks.append(_chk(
        "casemix_common_support_gate",
        "门禁：H6 独有罕见组(4例)与 H7 未覆盖体检例不外推、不判可疑",
        support_ok,
        "CG-RARE 共同支持失败、EP-H7-101 未覆盖；两条路线均排除且门禁登记",
        {"support_failures": sorted(fail_groups),
         "uncovered": sorted(uncovered_codes),
         "direct_supported_groups": direct["supported_groups"],
         "matching_excluded": sorted(m_excluded),
         "gated": sorted(gated_episodes)}))

    # 13) 小组病例区间：t 分位加宽且不输出可疑判定 -------------------------
    # 取直接标准化分组明细中带 small_group 标记的单元（H7 重症组 20<30 阈值）
    small_cells = [b for r in direct["orgs"] for b in r["group_breakdown"]
                   if b["small_group"]]
    # 每例区间半宽 = t·SD/√n：样本越少区间越宽（t 分位行为）
    wide_n, narrow_n = 5, 25
    wide_half = cm.t_critical(wide_n - 1) * 200.0 / math.sqrt(wide_n)
    narrow_half = cm.t_critical(narrow_n - 1) * 200.0 / math.sqrt(narrow_n)
    # 共同支持失败的罕见组在主结果中必须整体排除（不产生任何期望区间）
    rare_in_breakdown = [b for r in direct["orgs"] for b in r["group_breakdown"]
                         if b["group_code"] == "CG-RARE"]
    # 匹配路线对被支持的主样本机构结果：区间为有限正数、且残差标记不是可疑
    flags = {r["org_code"]: r["residual"]["flag"] for r in matching["orgs"]}
    small_ok = (
        cm.t_critical(4) > cm.t_critical(24) > 1.96
        and wide_half > narrow_half
        and not rare_in_breakdown
        and len(small_cells) >= 1
        and all(f != "out_of_interval_review" for f in flags.values())
        and all(g["action"] for g in gates)
    )
    checks.append(_chk(
        "casemix_small_group_interval",
        "小组病例：t 分位区间随小样本加宽；共同支持失败组不产生区间、不判可疑",
        small_ok,
        "t(4)>t(24)>1.96；每例半宽 n=5 宽于 n=25；CG-RARE 无区间；H7 重症组标小组；无可疑标记",
        {"t_df4": round(cm.t_critical(4), 3), "t_df24": round(cm.t_critical(24), 3),
         "per_case_half_n5": round(wide_half, 2),
         "per_case_half_n25": round(narrow_half, 2),
         "small_cells": [(r["org_code"], b["group_code"], b["cases"])
                         for r in direct["orgs"] for b in r["group_breakdown"]
                         if b["small_group"]],
         "rare_breakdown_rows": len(rare_in_breakdown),
         "matching_flags": flags}))

    passed = sum(1 for c in checks if c["passed"])
    return {"overall": passed == len(checks), "passed": passed,
            "total": len(checks), "checks": checks}
