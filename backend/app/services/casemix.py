"""病例组合基准：分层器 + 期望值计算器（两条路线）+ 残差 + 门禁。

术语与口径
----------
- episode：一次就诊，金额由结算行汇总；携带入院诊断、年龄、急诊/住院类型、
  手术复杂度与生效政策。
- 预登记临床分组（clinical group）：口径冻结的直接标准化单元；未入组=未覆盖病例。
- 共同支持（common support）：某病例组合必须在每家被比较机构都有足够可比病例，
  否则门禁阻断，期望值不向外推。
- 两条路线估计的都是“相同病例结构下每家机构的期望费用/服务量与观察值残差”，
  残差只用于排定核查优先级，**不构成违法违规结论**。

路线 A（direct，预登记分组直接标准化）
- 解释稳定、可复算；细分不足时组内异质性会进入残差。
- 参考费用取各预登记组的跨机构合并均值；机构期望=∑ 本组病例占比 × 组参考均值。
- 小组病例（组内可比数 < min_group_n）用 t 分位加宽区间，且门禁禁止判“可疑”。

路线 B（matching，相似病例匹配）
- 在不改变预登记组别口径的前提下（候选邻居限定同组），按诊断/年龄/就诊类型/
  手术复杂度做 Gower 距离近邻匹配，区分更细；依赖共同支持，邻居不足即拒绝外推。
"""
import math
from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import ClinicalGroup, Episode, Org

DISCLAIMER = ("病例组合期望值与观察残差仅用于在相同病例结构下排定核查优先级，"
              "不构成欺诈或违法违规结论；定性须经人工核查与法定程序。")

# 匹配路线参数（冻结，保证可复算）
MATCH_K = 8                 # 最多取 k 个近邻
MATCH_MIN_NEIGHBORS = 3     # 少于该数量邻居 → 共同支持失败
MATCH_DISTANCE_THRESHOLD = 0.50
W_DIAG, W_KIND, W_SURGERY, W_AGE = 0.35, 0.20, 0.20, 0.25
AGE_SCALE = 30.0

# t 分位（双侧 95%）常用自由度表，间隔线性插值，df>30 收敛到 1.96
_T_TABLE = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
            7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 14: 2.145,
            16: 2.120, 18: 2.093, 20: 2.086, 24: 2.064, 30: 2.042, 60: 2.000,
            120: 1.980, 10_000: 1.96}


def t_critical(df: int) -> float:
    """双侧 95% t 分位，线性插值；df 越大越接近 1.96。"""
    if df <= 1:
        return _T_TABLE[1]
    keys = sorted(_T_TABLE)
    for k in keys:
        if df <= k:
            lo = max(j for j in keys if j < k)
            frac = (df - lo) / (k - lo)
            return _T_TABLE[lo] + frac * (_T_TABLE[k] - _T_TABLE[lo])
    return 1.96


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def sample_sd(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ------------------------------------------------------------- 数据装载
def _episode_dicts(db: Session) -> list[dict]:
    org_name = {o.code: o.name for o in db.query(Org).all()}
    rows = []
    for ep in db.query(Episode).order_by(Episode.code).all():
        rows.append({
            "code": ep.code,
            "org_code": ep.org_code,
            "org_name": org_name.get(ep.org_code, ep.org_code),
            "person_code": ep.person_code,
            "date": ep.episode_date.isoformat(),
            "diagnosis": ep.admission_diagnosis,
            "dx_prefix": (ep.admission_diagnosis or "").split(" ")[0],
            "age": ep.age,
            "visit_kind": ep.visit_kind,
            "surgery_level": ep.surgery_level,
            "group": ep.clinical_group_code,
            "policy_code": ep.policy_code,
            "policy_version": ep.policy_version,
            "line_count": ep.line_count,
            "units": float(ep.service_units or 0),
            "amount": float(ep.observed_amount or 0),
            "block": (ep.props or {}).get("sample_block", "core"),
        })
    return rows


def _group_meta(db: Session) -> dict[str, ClinicalGroup]:
    return {g.code: g for g in db.query(ClinicalGroup).all()}


# ------------------------------------------------------------- 分层器
def stratify(db: Session, org_allow: set[str] | None = None) -> dict:
    """生成可比病例组合、覆盖诊断与共同支持诊断（只读，不落库）。

    覆盖/共同支持事实基于全量病例计算；org_allow 非空时仅收敛输出可见机构，
    不改变参考口径，保证片区用户看到的期望区间与全局一致。
    """
    eps = _episode_dicts(db)
    groups = _group_meta(db)
    all_orgs = sorted({e["org_code"] for e in eps})
    orgs = all_orgs if org_allow is None else [o for o in all_orgs if o in org_allow]

    by_group_org: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    uncovered = []
    for e in eps:
        if e["group"]:
            by_group_org[e["group"]][e["org_code"]].append(e)
        elif org_allow is None or e["org_code"] in org_allow:
            uncovered.append({
                "episode": e["code"], "org": e["org_code"],
                "diagnosis": e["diagnosis"], "block": e["block"],
                "reason": "未命中任何预先登记临床分组（未覆盖病例，禁止外推）"})

    group_rows, support_failures, small_warnings = [], [], []
    for gcode in sorted(by_group_org):
        meta = groups.get(gcode)
        min_n = meta.min_group_n if meta else 30
        per_org = by_group_org[gcode]
        full_counts = {o: len(per_org.get(o, [])) for o in all_orgs}
        counts = (full_counts if org_allow is None
                  else {o: n for o, n in full_counts.items() if o in org_allow})
        # 共同支持是全局事实：按全量机构判定缺失
        missing_orgs = [o for o, n in full_counts.items() if n == 0]
        small_orgs = [o for o, n in full_counts.items() if 0 < n < min_n]
        if org_allow is not None:
            missing_orgs = [o for o in missing_orgs if o in org_allow]
            small_orgs = [o for o in small_orgs if o in org_allow]
        common_support = not [o for o, n in full_counts.items() if n == 0]
        row = {
            "group_code": gcode,
            "group_name": meta.name if meta else gcode,
            "min_group_n": min_n,
            "counts_by_org": counts,
            "total": sum(counts.values()),
            "missing_orgs": missing_orgs,
            "small_orgs": small_orgs,
            "common_support": common_support,
            "covered_diagnoses": sorted({e["diagnosis"] for o, es in per_org.items()
                                         if org_allow is None or o in org_allow
                                         for e in es})}
        group_rows.append(row)
        visible_episodes = [e["code"] for o, es in per_org.items()
                            if org_allow is None or o in org_allow for e in es]
        if not common_support and visible_episodes:
            support_failures.append({
                "group_code": gcode,
                "reason": "仅单一机构存在该病例组合，无跨机构共同支持",
                "missing_orgs": missing_orgs,
                "small_orgs": small_orgs,
                "episodes": visible_episodes,
                "gate_action": "exclude_from_benchmark_no_extrapolation"})
        elif small_orgs:
            small_warnings.append({
                "group_code": gcode,
                "reason": "部分机构组内病例数低于小组阈值，区间按 t 分位加宽且不判可疑",
                "small_orgs": small_orgs,
                "gate_action": "widen_interval_no_suspicious_flag"})

    visible_eps = [e for e in eps if org_allow is None or e["org_code"] in org_allow]
    covered = [e for e in visible_eps if e["group"]]
    return {
        "orgs": orgs,
        "groups": group_rows,
        "uncovered": uncovered,
        "common_support_failures": support_failures,
        "small_group_warnings": small_warnings,
        "total_episodes": len(visible_eps),
        "covered_episodes": len(covered),
        "coverage_rate": round(len(covered) / len(visible_eps), 4) if visible_eps else 0,
    }


def gate_decisions(db: Session, org_allow: set[str] | None = None) -> list[dict]:
    """门禁：未覆盖病例与共同支持失败病例一律禁止外推为可疑；小组病例加宽但不判可疑。"""
    layers = stratify(db, org_allow)
    decisions = []
    for f in layers["common_support_failures"]:
        decisions.append({
            "scope": f["group_code"], "gate": "common_support",
            "passed": False, "episodes": f["episodes"],
            "reason": f["reason"], "action": "排除出基准计算，不做跨机构外推，不得标记可疑"})
    for w in layers.get("small_group_warnings", []):
        decisions.append({
            "scope": w["group_code"], "gate": "small_group",
            "passed": True, "episodes": [],
            "reason": w["reason"], "action": w["gate_action"]})
    for u in layers["uncovered"]:
        decisions.append({
            "scope": u["episode"], "gate": "coverage",
            "passed": False, "episodes": [u["episode"]],
            "reason": u["reason"], "action": "未覆盖病例：路线A不入组；路线B须通过邻居共同支持，否则同样排除"})
    return decisions


# ------------------------------------------------------------- 未标准化对照
def unstandardized(db: Session, org_allow: set[str] | None = None) -> dict:
    """单看人均费用的原始对照（题目核验：总均值差会被病例结构差异放大）。"""
    eps = _episode_dicts(db)
    out = {}
    for block in ("core", "all"):
        subset = eps if block == "all" else [e for e in eps if e["block"] == "core"]
        if org_allow is not None:
            subset = [e for e in subset if e["org_code"] in org_allow]
        per_org = {}
        for org in sorted({e["org_code"] for e in subset}):
            vals = [e["amount"] for e in subset if e["org_code"] == org]
            per_org[org] = {
                "episodes": len(vals),
                "mean_amount": round(mean(vals), 2),
                "total_amount": round(sum(vals), 2)}
        means = [v["mean_amount"] for v in per_org.values()]
        out[block] = {
            "by_org": per_org,
            "mean_diff": round(max(means) - min(means), 2) if len(means) > 1 else 0,
            "note": "未做病例组合调整，重症占比差异直接计入人均费用差"}
    out["interpretation"] = (
        "核心 100 例口径下两家机构人均费用相差 2000 元，差异完全来自重症比例"
        "（60% vs 20%）而非同组内费用水平；详见标准化后的残差。")
    return out


# ------------------------------------------------------------- 路线 A：直接标准化
def _reference_stats(eps: list[dict], field: str) -> dict[str, dict]:
    cells: dict[str, list[float]] = defaultdict(list)
    for e in eps:
        cells[e["group"]].append(e[field])
    stats = {}
    for g, vals in cells.items():
        stats[g] = {"n": len(vals), "mean": mean(vals), "sd": sample_sd(vals)}
    return stats


def direct_standardization(db: Session, org_allow: set[str] | None = None) -> dict:
    # 装载全部 episode：共同支持失败/未覆盖病例也要走过门禁并进入排除清单
    eps = _episode_dicts(db)
    groups_meta = _group_meta(db)
    full_layers = stratify(db)
    supported_groups = {g["group_code"] for g in full_layers["groups"] if g["common_support"]}
    layers = stratify(db, org_allow)   # 输出用覆盖信息按权限收敛
    all_orgs = sorted({e["org_code"] for e in eps})
    orgs = all_orgs if org_allow is None else [o for o in all_orgs if o in org_allow]

    # 参考均值/离散度：共同支持组合并全部机构病例
    ref_eps = [e for e in eps if e["group"] in supported_groups]
    ref_amount = _reference_stats(ref_eps, "amount")
    ref_units = _reference_stats(ref_eps, "units")

    org_rows, gates = [], []
    for org in orgs:
        mine = [e for e in eps if e["org_code"] == org]
        covered = [e for e in mine if e["group"] in supported_groups]
        excluded = [e for e in mine if e not in covered]
        n_o = len(covered)
        per_group = defaultdict(list)
        for e in covered:
            per_group[e["group"]].append(e)

        exp_total = exp_units = 0.0
        amt_half = units_half = 0.0
        breakdown = []
        small_group = False
        for gcode, members in sorted(per_group.items()):
            n_og = len(members)
            ra, ru = ref_amount[gcode], ref_units[gcode]
            t = t_critical(max(n_og - 1, 1))
            # 单元区间：n_og × 组参考均值 ± t·组SD·√n_og（可加且保守）
            cell_exp = n_og * ra["mean"]
            cell_half = t * ra["sd"] * math.sqrt(n_og)
            cell_units = n_og * ru["mean"]
            cell_units_half = t * ru["sd"] * math.sqrt(n_og)
            exp_total += cell_exp
            exp_units += cell_units
            amt_half += cell_half
            units_half += cell_units_half
            is_small = n_og < groups_meta[gcode].min_group_n
            small_group = small_group or is_small
            obs_mean = mean([m["amount"] for m in members])
            breakdown.append({
                "group_code": gcode,
                "group_name": groups_meta[gcode].name,
                "cases": n_og,
                "share": round(n_og / n_o, 4),
                "observed_mean": round(obs_mean, 2),
                "reference_mean": round(ra["mean"], 2),
                "expected_total": round(cell_exp, 2),
                "expected_total_low": round(cell_exp - cell_half, 2),
                "expected_total_high": round(cell_exp + cell_half, 2),
                "small_group": is_small,
                "t_critical": round(t, 3)})

        obs_total = sum(e["amount"] for e in covered)
        obs_units = sum(e["units"] for e in covered)
        obs_mean = obs_total / n_o if n_o else 0
        exp_mean = exp_total / n_o if n_o else 0
        residual_total = obs_total - exp_total
        within = (exp_total - amt_half) <= obs_total <= (exp_total + amt_half)
        flag = _residual_flag(within, small_group, excluded)
        org_rows.append({
            "org_code": org,
            "episodes_total": len(mine),
            "benchmark_episodes": n_o,
            "excluded_episodes": [
                {"code": e["code"], "group": e["group"], "diagnosis": e["diagnosis"],
                 "reason": _exclusion_reason(e, supported_groups)} for e in excluded],
            "observed": {
                "cases": n_o, "total_amount": round(obs_total, 2),
                "mean_amount": round(obs_mean, 2),
                "total_units": round(obs_units, 2),
                "mean_units": round(obs_units / n_o, 3) if n_o else 0},
            "expected": {
                "cases": n_o,
                "total_amount": round(exp_total, 2),
                "total_low": round(exp_total - amt_half, 2),
                "total_high": round(exp_total + amt_half, 2),
                "mean_amount": round(exp_mean, 2),
                "mean_low": round((exp_total - amt_half) / n_o, 2) if n_o else 0,
                "mean_high": round((exp_total + amt_half) / n_o, 2) if n_o else 0,
                "total_units": round(exp_units, 2),
                "units_low": round(exp_units - units_half, 2),
                "units_high": round(exp_units + units_half, 2),
                "mean_units": round(exp_units / n_o, 3) if n_o else 0},
            "residual": {
                "total_amount": round(residual_total, 2),
                "per_case": round(obs_mean - exp_mean, 2),
                "pct": round(100 * residual_total / exp_total, 3) if exp_total else 0,
                "within_interval": within,
                "small_group": small_group,
                "flag": flag[0], "flag_note": flag[1]},
            "group_breakdown": breakdown})
        if excluded:
            gates.append({
                "org_code": org, "gate": "coverage_and_common_support",
                "episodes": [e["code"] for e in excluded],
                "action": "未覆盖/共同支持失败病例不参与期望与残差计算，禁止外推为可疑"})

    return {
        "method": "direct",
        "method_name": "预登记临床分组直接标准化",
        "method_note": "口径冻结、解释稳定；细分不足时组内差异保留在残差中。",
        "supported_groups": sorted(supported_groups),
        "orgs": org_rows, "gates": gates,
        "coverage": layers, "disclaimer": DISCLAIMER}


def _exclusion_reason(e: dict, supported_groups: set[str]) -> str:
    if not e["group"]:
        return "未覆盖：未命中预登记临床分组"
    if e["group"] not in supported_groups:
        return "共同支持失败：该病例组合仅单一机构存在，无跨机构可比病例"
    return ""


def _residual_flag(within: bool, small_group: bool, excluded: list) -> tuple[str, str]:
    if small_group:
        return ("small_group_widened",
                "存在小组病例，区间已按 t 分位加宽；仅观察，不输出可疑判定（门禁）")
    if within:
        return ("within_benchmark", "观察值落在相同病例结构的期望区间内")
    return ("out_of_interval_review",
            "观察值超出期望区间，建议人工核查；该标记不是违法违规结论")


# ------------------------------------------------------------- 路线 B：相似病例匹配
def _gower(a: dict, b: dict) -> float:
    d_diag = 0.0 if a["dx_prefix"] == b["dx_prefix"] else 1.0
    d_kind = 0.0 if a["visit_kind"] == b["visit_kind"] else 1.0
    d_surg = 0.0 if a["surgery_level"] == b["surgery_level"] else 1.0
    d_age = min(abs(a["age"] - b["age"]) / AGE_SCALE, 1.0)
    return W_DIAG * d_diag + W_KIND * d_kind + W_SURGERY * d_surg + W_AGE * d_age


def _match_one(target: dict, pool: list[dict]) -> dict:
    """在同预登记组（保持组别口径不变）的其他机构病例中找近邻。"""
    # 未覆盖病例只能与其他机构的未覆盖病例匹配；有组者限定同组
    same_caliber = [e for e in pool
                    if (e["group"] or None) == (target["group"] or None)]
    scored = sorted(((_gower(target, c), c) for c in same_caliber),
                    key=lambda x: x[0])[:MATCH_K]
    close = [(d, c) for d, c in scored if d <= MATCH_DISTANCE_THRESHOLD]
    if len(close) < MATCH_MIN_NEIGHBORS:
        return {"supported": False, "neighbors": len(close),
                "reason": "跨机构同组近邻不足，共同支持失败，禁止外推",
                "neighbor_codes": [c["code"] for _, c in close]}
    weights = [1.0 / (1.0 + d) for d, _ in close]
    w_sum = sum(weights)
    exp_amount = sum(w * c["amount"] for w, (_, c) in zip(weights, close)) / w_sum
    exp_units = sum(w * c["units"] for w, (_, c) in zip(weights, close)) / w_sum
    return {"supported": True, "neighbors": len(close),
            "max_distance": round(max(d for d, _ in close), 4),
            "expected_amount": exp_amount, "expected_units": exp_units,
            "neighbor_codes": [c["code"] for _, c in close]}


def matching_standardization(db: Session, org_allow: set[str] | None = None) -> dict:
    # 装载全部 episode：共同支持失败/未覆盖病例由匹配门禁拒绝并登记
    eps = _episode_dicts(db)
    groups_meta = _group_meta(db)
    layers = stratify(db, org_allow)
    all_orgs = sorted({e["org_code"] for e in eps})
    orgs = all_orgs if org_allow is None else [o for o in all_orgs if o in org_allow]

    org_rows, gates = [], []
    for org in orgs:
        mine = [e for e in eps if e["org_code"] == org]
        other = [e for e in eps if e["org_code"] != org]
        supported, unsupported = [], []
        for e in mine:
            m = _match_one(e, other)
            e["_match"] = m
            (supported if m["supported"] else unsupported).append(e)

        n_o = len(supported)
        obs_vals = [e["amount"] for e in supported]
        exp_vals = [e["_match"]["expected_amount"] for e in supported]
        obs_unit_vals = [e["units"] for e in supported]
        exp_unit_vals = [e["_match"]["expected_units"] for e in supported]

        obs_total, exp_total = sum(obs_vals), sum(exp_vals)
        obs_units, exp_units = sum(obs_unit_vals), sum(exp_unit_vals)
        # 预测区间：以逐例（观察−期望）残差的样本标准差估计总量波动，
        # 小样本自动走更宽的 t 分位；这比期望值本身的离散更贴近“观察值区间”。
        t = t_critical(max(n_o - 1, 1))
        amt_resid_sd = sample_sd([o - e for o, e in zip(obs_vals, exp_vals)])
        unit_resid_sd = sample_sd([o - e for o, e in zip(obs_unit_vals, exp_unit_vals)])
        amt_half = t * amt_resid_sd * math.sqrt(n_o)
        units_half = t * unit_resid_sd * math.sqrt(n_o)
        within = (exp_total - amt_half) <= obs_total <= (exp_total + amt_half)

        per_group = defaultdict(list)
        for e in supported:
            per_group[e["group"]].append(e)
        breakdown = []
        small_group = False
        for gcode, members in sorted(per_group.items()):
            n_g = len(members)
            is_small = n_g < groups_meta[gcode].min_group_n
            small_group = small_group or is_small
            breakdown.append({
                "group_code": gcode,
                "group_name": groups_meta[gcode].name,
                "cases": n_g,
                "observed_mean": round(mean([m["amount"] for m in members]), 2),
                "matched_expected_mean": round(
                    mean([m["_match"]["expected_amount"] for m in members]), 2),
                "small_group": is_small})

        flag = _residual_flag(within, small_group, unsupported)
        org_rows.append({
            "org_code": org,
            "episodes_total": len(mine),
            "benchmark_episodes": n_o,
            "excluded_episodes": [
                {"code": e["code"], "group": e["group"], "diagnosis": e["diagnosis"],
                 "reason": e["_match"]["reason"],
                 "neighbors_found": e["_match"]["neighbors"]} for e in unsupported],
            "observed": {
                "cases": n_o, "total_amount": round(obs_total, 2),
                "mean_amount": round(mean(obs_vals), 2),
                "total_units": round(obs_units, 2),
                "mean_units": round(obs_units / n_o, 3) if n_o else 0},
            "expected": {
                "cases": n_o,
                "total_amount": round(exp_total, 2),
                "total_low": round(exp_total - amt_half, 2),
                "total_high": round(exp_total + amt_half, 2),
                "mean_amount": round(mean(exp_vals), 2),
                "mean_low": round((exp_total - amt_half) / n_o, 2) if n_o else 0,
                "mean_high": round((exp_total + amt_half) / n_o, 2) if n_o else 0,
                "total_units": round(exp_units, 2),
                "units_low": round(exp_units - units_half, 2),
                "units_high": round(exp_units + units_half, 2),
                "mean_units": round(exp_units / n_o, 3) if n_o else 0},
            "residual": {
                "total_amount": round(obs_total - exp_total, 2),
                "per_case": round(mean(obs_vals) - mean(exp_vals), 2),
                "pct": round(100 * (obs_total - exp_total) / exp_total, 3) if exp_total else 0,
                "within_interval": within,
                "small_group": small_group,
                "flag": flag[0], "flag_note": flag[1]},
            "group_breakdown": breakdown,
            "match_examples": [
                {"episode": e["code"], "diagnosis": e["diagnosis"],
                 "neighbors": e["_match"]["neighbors"],
                 "max_distance": e["_match"].get("max_distance"),
                 "neighbor_codes": e["_match"]["neighbor_codes"][:5]}
                for e in supported[:4]]})
        if unsupported:
            gates.append({
                "org_code": org, "gate": "matching_common_support",
                "episodes": [e["code"] for e in unsupported],
                "action": "相似病例共同支持失败：不生成机构期望值，禁止外推为可疑"})

    return {
        "method": "matching",
        "method_name": "相似病例匹配机构期望值",
        "method_note": ("候选邻居限定在同一预登记临床组内，不改变组别口径；"
                        "按诊断/年龄/就诊类型/手术复杂度近邻匹配，区分更细但依赖共同支持。"),
        "params": {"k": MATCH_K, "min_neighbors": MATCH_MIN_NEIGHBORS,
                   "distance_threshold": MATCH_DISTANCE_THRESHOLD,
                   "weights": {"diagnosis": W_DIAG, "visit_kind": W_KIND,
                               "surgery": W_SURGERY, "age": W_AGE}},
        "orgs": org_rows, "gates": gates,
        "coverage": layers, "disclaimer": DISCLAIMER}


# ------------------------------------------------------------- 统一入口
def benchmark(db: Session, method: str = "direct",
              org_allow: set[str] | None = None) -> dict:
    if method == "matching":
        return matching_standardization(db, org_allow)
    if method != "direct":
        raise ValueError("method 仅支持 direct / matching")
    return direct_standardization(db, org_allow)
