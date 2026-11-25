"""病例组合基准演示数据（全部虚构）。

样本设计（题目核验口径）：
- 两家医院 H6（康平区人民医院）/ H7（安宁区人民医院）各 100 次就诊 episode；
- H6 重症占比 60%（60 重症 / 40 普通），H7 重症占比 20%（20 重症 / 80 普通）；
- 重症组单例费用 8000 元、普通组 3000 元；组内做“对称抖动”（半数 +δ、半数 -δ），
  组均值严格保持 8000/3000，从而未经标准化的机构人均费用恰为 6000 与 4000（差 2000），
  而标准化后两组机构口径一致、残差≈0。
- 每个 episode 由多张结算行（claim line）汇总，携带入院诊断、年龄、急诊/住院类型、
  手术复杂度与生效政策版本。
- 共同支持失败样例：H6 独有的罕见重症联合手术组 CG-RARE（4 例，H7 0 例），
  以及 H7 独有、未入任何预登记分组的健康体检 episode（Z00，1 例）；门禁必须阻断外推。
"""
import random
from datetime import date as D
from datetime import datetime

from .models import (
    ClaimLine,
    ClinicalGroup,
    Episode,
    Org,
    Person,
    Policy,
    SourceRecord,
)

RNG = random.Random(20260331)

# 两组主样本的口径
SEVERE_COST = 8000.0
ROUTINE_COST = 3000.0
# 组内个体化扰动区间（与诊断/年龄/就诊类型/手术复杂度无关，模拟不可由分层特征
# 解释的组内差异）；按 (+m,-m) 成对生成，保证每组每机构均值严格等于基准费用
SEVERE_NOISE = (80.0, 260.0)
ROUTINE_NOISE = (60.0, 200.0)
RARE_COST = 22000.0
UNMATCHED_COST = 600.0

SEVERE_DX = ["I21 急性心肌梗死", "I50 心力衰竭", "I63 脑梗死", "J96 呼吸衰竭"]
ROUTINE_DX = ["I10 高血压", "E11 2型糖尿病", "J02 急性咽炎", "K52 胃肠炎"]

# 结算行结构（类别 → 费用占比；单元数为该类典型服务量）
SEVERE_LINE_SPEC = [
    ("手术麻醉", 0.45, 1.0),
    ("床位护理", 0.25, 8.0),
    ("检查检验", 0.15, 3.0),
    ("药品耗材", 0.15, 5.0),
]
ROUTINE_LINE_SPEC = [
    ("医疗服务", 0.40, 1.0),
    ("检查检验", 0.30, 2.0),
    ("药品耗材", 0.30, 3.0),
]
RARE_LINE_SPEC = [
    ("手术麻醉", 0.55, 2.0),
    ("床位护理", 0.20, 12.0),
    ("检查检验", 0.15, 6.0),
    ("药品耗材", 0.10, 8.0),
]


def _get_or_create_orgs(db):
    for code, name, region, level in [
        ("H6", "康平区人民医院", "CY", "三级综合"),
        ("H7", "安宁区人民医院", "HD", "三级综合"),
    ]:
        if not db.query(Org).filter_by(code=code).first():
            db.add(Org(code=code, name=name, org_type="hospital",
                       region=region, level=level))
    db.flush()


def _get_or_create_persons(db):
    existing = {p.code for p in db.query(Person).all()}
    for i in range(1, 121):
        code = f"CM-P{i:03d}"
        if code in existing:
            continue
        db.add(Person(code=code, name=f"病例组合参保人{i:03d}", pseudo_id=code,
                      gender="男" if i % 2 else "女", birth_year=1990 - (i % 45),
                      region="CY" if i % 2 else "HD"))
    db.flush()


def _get_or_create_policy(db):
    pol = db.query(Policy).filter_by(code="POL-CASEMIX").first()
    if pol:
        return pol
    pol = Policy(
        code="POL-CASEMIX", version="v2026.1",
        title="病例组合付费与机构费用基准管理办法",
        effective_from=D(2026, 1, 1), region="*",
        citation="医保办发〔2025〕31号 第7-10条",
        clauses={
            "benchmark_basis": "机构费用基准须按预登记临床分组或相似病例匹配做病例组合调整",
            "common_support": "仅单一机构出现的病例组合不得跨机构外推期望值",
            "small_group_threshold": 30,
            "small_group_rule": "组内可比病例<30 时区间加宽且不输出可疑判定",
            "residual_note": "期望残差仅用于排定核查优先级，不构成违法违规结论",
        })
    db.add(pol)
    db.flush()
    return pol


def _get_or_create_groups(db):
    if db.query(ClinicalGroup).count() > 0:
        return
    db.add_all([
        ClinicalGroup(
            code="CG-SEV", name="重症住院组", min_group_n=30,
            description="预先登记：重症入院诊断、住院/急诊、中高复杂度手术",
            rule={"diagnosis_prefixes": ["I21", "I50", "I63", "J96"],
                  "visit_kinds": ["住院", "急诊"],
                  "surgery_levels": ["moderate", "major"]}),
        ClinicalGroup(
            code="CG-ROU", name="普通常见病组", min_group_n=30,
            description="预先登记：常见慢病/轻症、门诊或急诊、无手术或小手术",
            rule={"diagnosis_prefixes": ["I10", "E11", "J02", "K52"],
                  "visit_kinds": ["普通门诊", "急诊"],
                  "surgery_levels": ["none", "minor"]}),
        ClinicalGroup(
            code="CG-RARE", name="罕见重症联合手术组", min_group_n=30,
            description="预先登记：先天性综合征伴重大联合手术；本批次仅 H6 有 4 例，无跨机构共同支持",
            rule={"diagnosis_prefixes": ["Q87"],
                  "visit_kinds": ["住院"],
                  "surgery_levels": ["major"]}),
    ])
    db.flush()


def _episode_source(db, ep_code, org, dx, amount):
    row = SourceRecord(
        code=f"SRC-{ep_code}", source_system=f"医保结算系统·{org}",
        record_type="casemix_episode", business_key=ep_code,
        valid_from=datetime(2026, 1, 5, 9, 0),
        received_at=datetime(2026, 1, 5, 18, 0),
        status="current",
        citation=f"{org} 就诊结算汇总单 {ep_code}（诊断 {dx}，合计 {amount:.2f} 元）",
        payload={"diagnosis": dx, "amount": amount})
    db.add(row)
    db.flush()
    return row


def _add_episode(db, idx, org, person, ep_date, dx, age, kind, surgery,
                 group_code, amount, line_spec, policy, block="core"):
    ep_code = f"EP-{org}-{idx:03d}"
    src = _episode_source(db, ep_code, org, dx, amount)
    ep = Episode(
        code=ep_code, org_code=org, person_code=person, episode_date=ep_date,
        admission_diagnosis=dx, age=age, visit_kind=kind, surgery_level=surgery,
        clinical_group_code=group_code, policy_code=policy.code,
        policy_version=policy.version, observed_amount=amount,
        source_id=src.id, props={"sample_block": block})
    db.add(ep)
    db.flush()

    units_total = 0.0
    running = 0.0
    for li, (category, share, units) in enumerate(line_spec, 1):
        if li < len(line_spec):
            line_amount = round(amount * share, 2)
            running += line_amount
        else:
            line_amount = round(amount - running, 2)  # 末行找平，保证合计精确
        units_total += units
        db.add(ClaimLine(
            code=f"{ep_code}-L{li}", episode_id=ep.id, line_no=f"{ep_code}-{li}",
            fee_item=f"{category}费用项{li}", category=category,
            units=units, amount=line_amount, source_id=src.id))
    ep.line_count = len(line_spec)
    ep.service_units = units_total
    return ep


def _paired_noises(n: int, low: float, high: float) -> list[float]:
    """生成 n 个均值严格为 0 的个体化扰动（n 须为偶数）：成对 (+m, -m)。"""
    out = []
    for _ in range(n // 2):
        m = RNG.uniform(low, high)
        out.extend([m, -m])
    return out


def _build_main_block(db, org, severe_n, routine_n, policy, start_idx):
    """一家医院的 100 次主样本就诊；组内对称噪声保证组均值严格不变。"""
    made = []
    severe_noises = _paired_noises(severe_n, *SEVERE_NOISE)
    routine_noises = _paired_noises(routine_n, *ROUTINE_NOISE)
    # ---- 重症
    for i in range(severe_n):
        idx = start_idx + i
        amount = SEVERE_COST + severe_noises[i]
        dx = SEVERE_DX[i % len(SEVERE_DX)]
        kind = "急诊" if i % 7 == 6 else "住院"
        surgery = "major" if i % 2 == 0 else "moderate"
        age = 62 + (i * 3) % 24           # 62..85，两家机构同年龄段分布
        ep_date = D.fromordinal(D(2026, 1, 5).toordinal() + (i * 2) % 70)
        person = f"CM-P{idx:03d}"
        made.append(_add_episode(
            db, idx, org, person, ep_date, dx, age, kind, surgery,
            "CG-SEV", amount, SEVERE_LINE_SPEC, policy))
    # ---- 普通
    for j in range(routine_n):
        idx = start_idx + severe_n + j
        amount = ROUTINE_COST + routine_noises[j]
        dx = ROUTINE_DX[j % len(ROUTINE_DX)]
        kind = "急诊" if j % 6 == 5 else "普通门诊"
        surgery = "minor" if j % 9 == 8 else "none"
        age = 31 + (j * 2) % 38           # 31..68，两家机构同年龄段分布
        ep_date = D.fromordinal(D(2026, 2, 1).toordinal() + (j * 2) % 55)
        person = f"CM-P{idx:03d}"
        made.append(_add_episode(
            db, idx, org, person, ep_date, dx, age, kind, surgery,
            "CG-ROU", amount, ROUTINE_LINE_SPEC, policy))
    return made


def _build_support_failures(db, policy):
    # H6 独有：罕见重症联合手术组 4 例（H7 为 0 例 → 无跨机构共同支持）
    for i in range(4):
        idx = 101 + i
        dx = f"Q87 先天性畸形综合征{i + 1}型"
        _add_episode(
            db, idx, "H6", f"CM-P{100 + i:03d}",
            D(2026, 3, 2 + i * 4), dx, age=1 + i, kind="住院",
            surgery="major", group_code="CG-RARE", amount=RARE_COST,
            line_spec=RARE_LINE_SPEC, policy=policy, block="support_failure")
    # H7 独有：健康体检，未入任何预登记分组（分层器标记未覆盖，匹配路线无 H6 同类邻居）
    _add_episode(
        db, 101, "H7", "CM-P118", D(2026, 3, 12), "Z00 健康体检",
        age=25, kind="普通门诊", surgery="none", group_code=None,
        amount=UNMATCHED_COST, line_spec=ROUTINE_LINE_SPEC, policy=policy,
        block="support_failure")


def seed_casemix(db):
    _get_or_create_orgs(db)
    _get_or_create_persons(db)
    policy = _get_or_create_policy(db)
    _get_or_create_groups(db)
    if db.query(Episode).count() > 0:
        return
    _build_main_block(db, "H6", severe_n=60, routine_n=40, policy=policy, start_idx=1)
    _build_main_block(db, "H7", severe_n=20, routine_n=80, policy=policy, start_idx=1)
    _build_support_failures(db, policy)
    db.flush()
