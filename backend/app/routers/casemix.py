"""病例组合基准 API：分层覆盖、未标准化对照、两条路线期望/残差、门禁与明细。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ClaimLine, ClinicalGroup, Episode, Org
from ..security import current_user
from ..services import casemix

router = APIRouter(prefix="/api/casemix", tags=["casemix"],
                   dependencies=[Depends(current_user)])


def _visible_orgs(db: Session, user) -> set[str] | None:
    """按统筹区权限收敛可见机构；admin / 全区通配返回 None 表示不限制。

    参考费用与共同支持仍基于全量病例计算，仅把输出限制在用户可见机构，
    保证片区用户看到的期望区间口径与全局一致且不泄露其他片区明细。
    """
    if user.role == "admin" or "*" in (user.region_scope or []):
        return None
    return {o.code for o in db.query(Org).all()
            if o.region in (user.region_scope or [])}


@router.get("/overview")
def overview(db: Session = Depends(get_db), user=Depends(current_user)):
    allowed = _visible_orgs(db, user)
    q = db.query(Episode)
    if allowed is not None:
        q = q.filter(Episode.org_code.in_(allowed))
    eps = q.all()
    covered = [e for e in eps if e.clinical_group_code]
    by_org: dict = {}
    for e in eps:
        d = by_org.setdefault(e.org_code, {"episodes": 0, "covered": 0,
                                           "amount": 0.0, "lines": 0})
        d["episodes"] += 1
        d["covered"] += 1 if e.clinical_group_code else 0
        d["amount"] += float(e.observed_amount)
        d["lines"] += e.line_count
    for d in by_org.values():
        d["mean_amount"] = round(d["amount"] / d["episodes"], 2) if d["episodes"] else 0
        d["amount"] = round(d["amount"], 2)
    return {
        "total_episodes": len(eps),
        "covered_episodes": len(covered),
        "coverage_rate": round(len(covered) / len(eps), 4) if eps else 0,
        "total_claim_lines": sum(e.line_count for e in eps),
        "groups": [{"code": g.code, "name": g.name, "min_group_n": g.min_group_n}
                   for g in db.query(ClinicalGroup).order_by(ClinicalGroup.id).all()],
        "by_org": by_org,
        "disclaimer": casemix.DISCLAIMER}


@router.get("/stratify")
def stratify(db: Session = Depends(get_db), user=Depends(current_user)):
    return casemix.stratify(db, _visible_orgs(db, user))


@router.get("/gates")
def gates(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = casemix.gate_decisions(db, _visible_orgs(db, user))
    return {"items": rows, "disclaimer": casemix.DISCLAIMER}


@router.get("/unstandardized")
def unstandardized(db: Session = Depends(get_db), user=Depends(current_user)):
    return casemix.unstandardized(db, _visible_orgs(db, user))


@router.get("/benchmark")
def benchmark(method: str = "direct", db: Session = Depends(get_db),
              user=Depends(current_user)):
    if method not in ("direct", "matching"):
        raise HTTPException(status_code=400, detail="method 仅支持 direct / matching")
    return casemix.benchmark(db, method, _visible_orgs(db, user))


@router.get("/episodes")
def episodes(org: str | None = None, group: str | None = None,
             covered: bool | None = None, db: Session = Depends(get_db),
             user=Depends(current_user)):
    allowed = _visible_orgs(db, user)
    q = db.query(Episode).order_by(Episode.code)
    if org:
        q = q.filter(Episode.org_code == org)
    elif allowed is not None:
        q = q.filter(Episode.org_code.in_(allowed))
    if group:
        q = q.filter(Episode.clinical_group_code == group)
    if covered is True:
        q = q.filter(Episode.clinical_group_code.isnot(None))
    if covered is False:
        q = q.filter(Episode.clinical_group_code.is_(None))
    items = [{
        "code": e.code, "org_code": e.org_code, "person_code": e.person_code,
        "date": e.episode_date.isoformat(),
        "diagnosis": e.admission_diagnosis, "age": e.age,
        "visit_kind": e.visit_kind, "surgery_level": e.surgery_level,
        "clinical_group_code": e.clinical_group_code,
        "policy": f"{e.policy_code}@{e.policy_version}",
        "line_count": e.line_count, "service_units": float(e.service_units),
        "observed_amount": float(e.observed_amount),
        "sample_block": (e.props or {}).get("sample_block", "core")}
        for e in q.all()]
    return {"items": items, "total": len(items)}


@router.get("/episodes/{code}")
def episode_detail(code: str, db: Session = Depends(get_db),
                   user=Depends(current_user)):
    ep = db.query(Episode).filter_by(code=code).first()
    if not ep:
        raise HTTPException(status_code=404, detail="episode 不存在")
    allowed = _visible_orgs(db, user)
    if allowed is not None and ep.org_code not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该统筹区数据")
    lines = db.query(ClaimLine).filter_by(episode_id=ep.id).order_by(ClaimLine.id).all()
    line_sum = round(sum(float(x.amount) for x in lines), 2)
    return {
        "episode": {
            "code": ep.code, "org_code": ep.org_code, "person_code": ep.person_code,
            "date": ep.episode_date.isoformat(),
            "diagnosis": ep.admission_diagnosis, "age": ep.age,
            "visit_kind": ep.visit_kind, "surgery_level": ep.surgery_level,
            "clinical_group_code": ep.clinical_group_code,
            "policy_code": ep.policy_code, "policy_version": ep.policy_version,
            "line_count": ep.line_count, "service_units": float(ep.service_units),
            "observed_amount": float(ep.observed_amount),
            "sample_block": (ep.props or {}).get("sample_block", "core")},
        "claim_lines": [{
            "code": x.code, "line_no": x.line_no, "fee_item": x.fee_item,
            "category": x.category, "units": float(x.units),
            "amount": float(x.amount)} for x in lines],
        "line_sum": line_sum,
        "aggregation_consistent": line_sum == round(float(ep.observed_amount), 2)}
