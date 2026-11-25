"""SQLAlchemy 数据模型。

设计要点：
- source_record 是“来源账”：每条业务记录都可回溯到来源系统、业务有效时间、
  接收时间以及撤销/更正链；业务表通过 source_id 引用。
- 身份证据分级（strong/weak），弱证据只产生 pending 候选，不自动合并个人。
- graph_node/graph_edge 是当前图；graph_snapshot 冻结截止日、身份簇版本、
  边范围与政策版本下的完整子图，供复演。
- case_judgment 是不可变判断版本，旧案件材料始终保留原判断来源。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ---------------------------------------------------------------- 用户与权限
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="auditor")  # admin/auditor
    # 数据权限：可看的统筹区与机构 code；["*"] 表示全部
    region_scope: Mapped[list] = mapped_column(JSONB, default=list)
    org_scope: Mapped[list] = mapped_column(JSONB, default=list)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------- 来源账
class SourceRecord(Base):
    __tablename__ = "source_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    source_system: Mapped[str] = mapped_column(String(64), index=True)
    record_type: Mapped[str] = mapped_column(String(48), index=True)
    business_key: Mapped[str] = mapped_column(String(128), index=True)
    # 业务有效时间
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 接收时间
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # current / corrected（被更正）/ revoked（被撤销）
    status: Mapped[str] = mapped_column(String(16), default="current", index=True)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_record.id"), nullable=True
    )
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可引用字段：凭证号/单据号/台账行号等，审计引用时展示
    citation: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


# ---------------------------------------------------------------- 组织/人员
class Org(Base):
    __tablename__ = "org"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    org_type: Mapped[str] = mapped_column(String(32))  # hospital/clinic/pharmacy/agency
    region: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Person(Base):
    __tablename__ = "person"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # P1...
    name: Mapped[str] = mapped_column(String(64))
    pseudo_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 医保伪标识
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[str] = mapped_column(String(32), default="")
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Practitioner(Base):
    __tablename__ = "practitioner"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # D1...
    name: Mapped[str] = mapped_column(String(64))
    credential_no: Mapped[str] = mapped_column(String(64), index=True)  # 执业凭证
    credential_valid_to: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"))
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IdentityEvidence(Base):
    """两个自然人之间的身份关联证据及其分级/裁定。"""
    __tablename__ = "identity_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    subject_type: Mapped[str] = mapped_column(String(16), default="person")  # person/practitioner
    left_code: Mapped[str] = mapped_column(String(32), index=True)
    right_code: Mapped[str] = mapped_column(String(32), index=True)
    # pseudo_id / professional_credential / shared_phone / shared_address / shared_agent
    evidence_type: Mapped[str] = mapped_column(String(40))
    evidence_value: Mapped[str] = mapped_column(String(256))
    tier: Mapped[str] = mapped_column(String(8))  # strong / weak
    valid_from: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    # auto=强证据自动成簇；pending=弱候选待核；confirmed/rejected=人工裁定
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class IdentityClusterVersion(Base):
    """身份簇的不可变版本。每次裁定/重算产生新版本号。"""
    __tablename__ = "identity_cluster_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reason: Mapped[str] = mapped_column(Text)
    # [{cluster: "C1", members: ["P1"]}, ...]
    clusters: Mapped[list] = mapped_column(JSONB, default=list)


# ---------------------------------------------------------------- 业务账
class Schedule(Base):
    __tablename__ = "schedule"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    practitioner_code: Mapped[str] = mapped_column(String(32), index=True)
    org_code: Mapped[str] = mapped_column(String(32))
    work_date: Mapped[datetime] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16))  # on / suspended（停诊）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Visit(Base):
    __tablename__ = "visit"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    org_code: Mapped[str] = mapped_column(String(32))
    practitioner_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visit_date: Mapped[datetime] = mapped_column(Date, index=True)
    visit_type: Mapped[str] = mapped_column(String(32), default="门诊")
    diagnosis: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Prescription(Base):
    __tablename__ = "prescription"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    visit_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    org_code: Mapped[str] = mapped_column(String(32))
    practitioner_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rx_no: Mapped[str] = mapped_column(String(64))
    rx_date: Mapped[datetime] = mapped_column(Date, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    items: Mapped[list["PrescriptionItem"]] = relationship(
        back_populates="prescription", cascade="all, delete-orphan"
    )


class PrescriptionItem(Base):
    __tablename__ = "prescription_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescription.id"))
    code: Mapped[str] = mapped_column(String(32), unique=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    ingredient: Mapped[str] = mapped_column(String(128), index=True)  # 有效成分
    spec: Mapped[str] = mapped_column(String(64))
    qty: Mapped[float] = mapped_column(Float)              # 总片/粒数（最小单位）
    daily_dose: Mapped[float] = mapped_column(Float)       # 每日用量
    days: Mapped[int] = mapped_column(Integer)             # 给药天数
    admin_start: Mapped[datetime] = mapped_column(Date)    # 给药期起
    admin_end: Mapped[datetime] = mapped_column(Date)       # 给药期止
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    prescription: Mapped[Prescription] = relationship(back_populates="items")


class Settlement(Base):
    __tablename__ = "settlement"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    settle_no: Mapped[str] = mapped_column(String(64))
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    rx_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pharmacy_code: Mapped[str] = mapped_column(String(32), index=True)
    settle_date: Mapped[datetime] = mapped_column(Date, index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    fund_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    self_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    # settled / refunded / partial_refund
    status: Mapped[str] = mapped_column(String(24), default="settled", index=True)
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    terminal_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Supply(Base):
    """药店供药记录。"""
    __tablename__ = "supply"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    pharmacy_code: Mapped[str] = mapped_column(String(32), index=True)
    settlement_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rx_item_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    ingredient: Mapped[str] = mapped_column(String(128), index=True)
    batch_no: Mapped[str] = mapped_column(String(64))
    qty: Mapped[float] = mapped_column(Float)
    supply_date: Mapped[datetime] = mapped_column(Date, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Delivery(Base):
    """配送签收。"""
    __tablename__ = "delivery"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    supply_code: Mapped[str] = mapped_column(String(32), index=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    receiver_name: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(256))
    signed_at: Mapped[datetime] = mapped_column(DateTime)
    signature_match: Mapped[bool] = mapped_column(Boolean, default=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class InventoryLedger(Base):
    """药店进销存台账行。"""
    __tablename__ = "inventory_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    pharmacy_code: Mapped[str] = mapped_column(String(32), index=True)
    ingredient: Mapped[str] = mapped_column(String(128), index=True)
    batch_no: Mapped[str] = mapped_column(String(64))
    # purchase_in / sale_out / transfer_out / transfer_in / return_in
    change_type: Mapped[str] = mapped_column(String(24), index=True)
    change_qty: Mapped[float] = mapped_column(Float)  # 入正出负
    ref_no: Mapped[str] = mapped_column(String(64))
    counterpart_org_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    txn_date: Mapped[datetime] = mapped_column(Date, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class StockTransfer(Base):
    """药店间调拨（解释表面重复购药的合法供药）。"""
    __tablename__ = "stock_transfer"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    transfer_no: Mapped[str] = mapped_column(String(64))
    from_pharmacy: Mapped[str] = mapped_column(String(32), index=True)
    to_pharmacy: Mapped[str] = mapped_column(String(32), index=True)
    ingredient: Mapped[str] = mapped_column(String(128))
    qty: Mapped[float] = mapped_column(Float)
    ship_date: Mapped[datetime] = mapped_column(Date)
    receive_date: Mapped[datetime] = mapped_column(Date)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class BedService(Base):
    """养老机构床位/服务记录（反证：老人在住、机构代办合法）。"""
    __tablename__ = "bed_service"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    agency_code: Mapped[str] = mapped_column(String(32), index=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    bed_no: Mapped[str] = mapped_column(String(32))
    service_from: Mapped[datetime] = mapped_column(Date)
    service_to: Mapped[datetime] = mapped_column(Date)
    service_type: Mapped[str] = mapped_column(String(64), default="机构住养")
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Referral(Base):
    __tablename__ = "referral"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    referral_no: Mapped[str] = mapped_column(String(64))
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    from_org: Mapped[str] = mapped_column(String(32))
    to_org: Mapped[str] = mapped_column(String(32))
    refer_date: Mapped[datetime] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(24), default="completed")
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Appeal(Base):
    """申诉材料与处理结果。"""
    __tablename__ = "appeal"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    appeal_no: Mapped[str] = mapped_column(String(64))
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    case_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text)
    # pending / reviewing / upheld（申诉成立）/ rejected
    result: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    drug_returned: Mapped[bool] = mapped_column(Boolean, default=False)
    return_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))


class Policy(Base):
    """医保政策及版本/有效日。"""
    __tablename__ = "policy"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    effective_from: Mapped[datetime] = mapped_column(Date)
    effective_to: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    region: Mapped[str] = mapped_column(String(32), default="*")
    clauses: Mapped[dict] = mapped_column(JSONB, default=dict)
    citation: Mapped[str] = mapped_column(String(256))


# ---------------------------------------------------------------- 图
class GraphNode(Base):
    __tablename__ = "graph_node"
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(80), unique=True, index=True)  # person:P1
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    ref_code: Mapped[str] = mapped_column(String(48), index=True)
    label: Mapped[str] = mapped_column(String(256))
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    org_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    props: Mapped[dict] = mapped_column(JSONB, default=dict)


class GraphEdge(Base):
    __tablename__ = "graph_edge"
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    edge_type: Mapped[str] = mapped_column(String(40), index=True)
    src_uid: Mapped[str] = mapped_column(String(80), index=True)
    dst_uid: Mapped[str] = mapped_column(String(80), index=True)
    # support（支持风险假设）/ refute（反驳/合法解释）/ neutral
    stance: Mapped[str] = mapped_column(String(8), default="neutral", index=True)
    basis: Mapped[str] = mapped_column(Text)  # 具体依据（人话）
    source_codes: Mapped[list] = mapped_column(JSONB, default=list)  # 可引用来源账 code
    event_date: Mapped[datetime | None] = mapped_column(Date, nullable=True, index=True)
    # active / negated（已被反证或裁定否定，不再用于新扩展）
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)
    props: Mapped[dict] = mapped_column(JSONB, default=dict)


class GraphSnapshot(Base):
    __tablename__ = "graph_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    cutoff_date: Mapped[datetime] = mapped_column(Date)
    identity_version: Mapped[int] = mapped_column(Integer)
    # 边范围：{default_edge_types, max_hops, window_days, profile, pattern_code?}
    edge_scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    policy_versions: Mapped[list] = mapped_column(JSONB, default=list)
    trigger_reason: Mapped[str] = mapped_column(Text)
    # 冻结的完整图（节点/边）与统计
    graph: Mapped[dict] = mapped_column(JSONB, default=dict)
    affected_case_codes: Mapped[list] = mapped_column(JSONB, default=list)


# ---------------------------------------------------------------- 模式与案件
class PatternConfig(Base):
    __tablename__ = "pattern_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    window_days: Mapped[int] = mapped_column(Integer, default=14)
    max_hops: Mapped[int] = mapped_column(Integer, default=2)
    allowed_edge_types: Mapped[list] = mapped_column(JSONB, default=list)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    high_amount_threshold: Mapped[float] = mapped_column(Numeric(14, 2), default=50000)
    # 高金额跨机构需要显式增加的边与截止条件
    extended_edge_types: Mapped[list] = mapped_column(JSONB, default=list)
    extension_cutoff: Mapped[str | None] = mapped_column(String(256), nullable=True)
    policy_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Case(Base):
    __tablename__ = "case"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # C-2026-001
    title: Mapped[str] = mapped_column(String(256))
    pattern_codes: Mapped[list] = mapped_column(JSONB, default=list)
    primary_person_code: Mapped[str] = mapped_column(String(32), index=True)
    # 模式分组锚点：person/rx/settlement/referral 等 code，保证可确定性复算
    anchors: Mapped[dict] = mapped_column(JSONB, default=dict)
    region: Mapped[str] = mapped_column(String(32), default="")
    org_codes: Mapped[list] = mapped_column(JSONB, default=list)
    # new / pending（待人工核查）/ reviewing / confirmed_risk_clue /
    # excluded_legitimate / closed_returned / closed_weak_identity
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    priority_rank_rule: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority_rank_model: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_score: Mapped[float] = mapped_column(Float, default=0)
    model_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_min: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    amount_max: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    fund_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    counter_completeness: Mapped[float] = mapped_column(Float, default=0)  # 0~1
    unresolved_counter: Mapped[int] = mapped_column(Integer, default=0)
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 逻辑引用当前判断版本；不加 FK 以避免 case <-> case_judgment 循环依赖
    current_judgment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("graph_snapshot.id"), nullable=True
    )
    affected: Mapped[bool] = mapped_column(Boolean, default=False)
    affected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CaseJudgment(Base):
    """不可变判断版本：规则事实/模型解释/投影子图/来源/快照全部冻结。"""
    __tablename__ = "case_judgment"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="auto")  # auto/manual/resnapshot
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    author: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_no: Mapped[int] = mapped_column(Integer)
    rule_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    model_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    counter_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    projection: Mapped[dict] = mapped_column(JSONB, default=dict)  # 最小子图
    amount_range: Mapped[dict] = mapped_column(JSONB, default=dict)
    disclaimer: Mapped[str] = mapped_column(
        String(256),
        default="本结果为风险线索优先级与核查建议，不构成欺诈或违法结论；定性须经人工核查与法定程序。",
    )
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_case_version"),)


class CaseReviewEvent(Base):
    __tablename__ = "case_review_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_code: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewer: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CounterCheck(Base):
    """反证编排器对每个案件执行的核查项与结论。"""
    __tablename__ = "counter_check"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_code: Mapped[str] = mapped_column(String(32), index=True)
    check_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(128))
    # checked_explains（材料给出合理解释）/ not_excluded（仍未排除）/
    # confirmed（反证成立，如退回）/ no_record（未查到材料）
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(Text)
    source_codes: Mapped[list] = mapped_column(JSONB, default=list)
    effect: Mapped[str] = mapped_column(String(16), default="none")  # downweight/close/none
    checked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ModelSample(Base):
    """图模型演练样本（带时间戳与标签，用于时间切分验证）。"""
    __tablename__ = "model_sample"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_code: Mapped[str] = mapped_column(String(32), unique=True)
    sample_date: Mapped[datetime] = mapped_column(Date, index=True)
    region: Mapped[str] = mapped_column(String(32))
    org_code: Mapped[str] = mapped_column(String(32))
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    label: Mapped[int] = mapped_column(Integer)  # 1=历史确认问题线索 0=排除/正常


class ModelRun(Base):
    __tablename__ = "model_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    train_end: Mapped[datetime] = mapped_column(Date)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    weights: Mapped[dict] = mapped_column(JSONB, default=dict)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[str] = mapped_column(Text)


# ============================================================
# 病例组合（Case-Mix）基准：结算行 → 就诊 episode → 分层/期望/残差
#
# 设计要点：
# - ClaimLine 是逐行结算账（可多行），Episode 是一次就诊的汇总单元，
#   金额由结算行汇总得到，并携带入院诊断、年龄、急诊/住院类型、手术复杂度
#   与生效政策，保证“单看人均费用”之外可做病例结构调整。
# - ClinicalGroup 是预先登记的临床分组（直接标准化口径），分组规则冻结、
#   口径稳定；Episode.clinical_group_code 记录入组结果，未入组=未覆盖病例。
# - 期望值与残差不落库，由 services/casemix.py 按两条路线实时计算，
#   保证政策/分组口径变更后可复算；门禁结果随 episode 覆盖状态返回。
# =================================================================
class ClinicalGroup(Base):
    """预先登记的临床分组（直接标准化的稳定口径）。"""
    __tablename__ = "clinical_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 入组规则（冻结口径）：{diagnosis_prefixes, visit_types, surgery_levels, age_min?, age_max?}
    rule: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 小组判定阈值：组内可比病例数低于该值时只给宽区间并标注 small_group
    min_group_n: Mapped[int] = mapped_column(Integer, default=30)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Episode(Base):
    """一次就诊 episode：由结算行汇总，关联诊断/年龄/就诊类型/手术复杂度/政策。"""
    __tablename__ = "episode"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # EP-H1-001
    org_code: Mapped[str] = mapped_column(String(32), index=True)
    person_code: Mapped[str] = mapped_column(String(32), index=True)
    episode_date: Mapped[datetime] = mapped_column(Date, index=True)
    admission_diagnosis: Mapped[str] = mapped_column(String(64), index=True)
    age: Mapped[int] = mapped_column(Integer)
    # 住院 / 急诊 / 普通门诊
    visit_kind: Mapped[str] = mapped_column(String(16), index=True)
    # 手术复杂度：none / minor / moderate / major
    surgery_level: Mapped[str] = mapped_column(String(16), index=True)
    # 预登记临床分组入组结果；None 表示未覆盖（门禁不得外推）
    clinical_group_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True)
    # 生效政策（按 episode 日期命中的政策版本）
    policy_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 服务量：结算行汇总得到的结算行数与服务单元数
    line_count: Mapped[int] = mapped_column(Integer, default=1)
    service_units: Mapped[float] = mapped_column(Float, default=1.0)
    # 观察费用（结算行金额合计）
    observed_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    props: Mapped[dict] = mapped_column(JSONB, default=dict)
    claim_lines: Mapped[list["ClaimLine"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan")


class ClaimLine(Base):
    """逐行结算账：多行汇总到同一就诊 episode。"""
    __tablename__ = "claim_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episode.id"), index=True)
    line_no: Mapped[str] = mapped_column(String(64))
    fee_item: Mapped[str] = mapped_column(String(128))
    # 费用类别：医疗服务 / 检查检验 / 药品耗材 / 手术麻醉 / 床位护理
    category: Mapped[str] = mapped_column(String(32), index=True)
    units: Mapped[float] = mapped_column(Float, default=1.0)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    source_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    episode: Mapped[Episode] = relationship(back_populates="claim_lines")
