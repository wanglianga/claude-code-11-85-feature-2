# 医保风险线索关系分析平台（RiskGraph）

面向医保稽核部门的**跨机构风险线索关系分析**演示系统：从就诊、处方、结算、药店库存/供药、
医师排班、转诊、养老床位与申诉材料中构建**可回溯、受限扩展、可反证、可复演**的关系图，
帮助稽核人员发现值得人工核验的跨机构异常。

> ⚖️ **合规红线**：规则分与图模型分都只是“风险结构 + 核查优先级”，**不是欺诈或违法结论**。
> 家庭共用联系方式、养老机构代办、慢病协作不会被直接解释为欺诈；弱身份证据不自动合并个人。
> 所有案件定性必须经人工核查与法定程序。

技术栈：**Vue 3 + Vite + Element Plus（SVG 自研图谱）｜ FastAPI + SQLAlchemy ｜ PostgreSQL**。
一键部署：**Docker Compose（PostgreSQL 16 + FastAPI 同源托管前端 dist）**。

---

## 1. 功能与题目要求对照

| 题目要求 | 实现位置 |
| --- | --- |
| 来源账：来源系统 / 业务有效时间 / 接收时间 / 撤销更正 / 可引用字段 | 表 `source_record`；页面 **来源账**；每条业务记录均挂 `source_id`，支持更正链 `supersedes` |
| 身份解析分级：伪标识、有效期内专业凭证=强；共享电话/地址/代办人=弱候选，不自动合并 | `services/identity.py`；页面 **身份解析**；弱证据仅 `pending`，可人工确认/驳回并产生簇新版本+快照 |
| 边保存具体依据、来源与支持/反驳方向 | 表 `graph_edge`：`basis / source_codes / stance(support/refute/neutral) / status(active/negated)` |
| 图快照：冻结截止日、身份簇版本、边范围、政策版本 | 表 `graph_snapshot`；页面 **快照复演**（冻结图谱、四要素、两快照 Diff） |
| 模式求值：多点重复购药 / 拆方 / 集中结算 / 异常转诊的必要事实 | `services/patterns.py`；页面 **模式与排序**、案件详情“必要事实” |
| 反证编排：进销存、配送签收、停诊、床位服务、政策、申诉结果 | `services/counterevidence.py`；页面 **反证联查** 与案件“反证”页签 |
| 案件投影：最小子图、金额范围、证据出处、人工状态；分数非结论 | `services/projection.py`；页面 **案件投影 / 案件详情**；按统筹区/机构做接口级权限 |
| 扩展范围=起始索赔/受益人+允许边；同伪标识/强簇、处方/结算/供药、两跳、180 天 | `services/expansion.py` 默认参数；页面 **受限图扩展** |
| 弱身份候选只生成待核边，不能继续扩展全网 | BFS 中弱边对端标记 `terminal_candidates`，不入队；验收 `p23_weak_no_expand` |
| 高金额跨机构扩展须模式配置增加边与截止条件并重新冻结快照 | `expansion.high_amount_extension` + `POST /api/graph/high-amount-extension`；C-001 被门槛拒绝、C-007 授权并可冻结 |
| 规则组合筛选风险结构 → 金额 + 反证完整度排序 | `services/ranking.py`，排序选项 `amount_completeness / amount_unresolved / rule` |
| 带时间切分验证的图模型：地区/机构偏差校准、高分可追溯路径与反证 | `services/graphmodel.py`（纯 Python 逻辑回归，train≤2026-01-31 / val=2026-02；验证集指标真实计算；地区+机构类型残差校准） |
| 规则与模型在 P1 上展示**相同支持链** | 模型结果直接回传规则引擎的 `support_chain` 为 `trace_chain_uids`；验收 `same_chain_and_rank` |
| 变更（身份裁定/政策有效日/退费更正）→ 新快照 + 标出受影响案件；旧材料保留原判断来源 | `routers/identity.py`、`routers/snapshots.py`；`case_judgment` 不可变版本，旧版本永久指向旧快照 |
| 病例组合基准：结算行汇总到 episode，关联诊断/年龄/急诊住院/手术复杂度/生效政策 | 表 `claim_line`/`episode`/`clinical_group`；`seed_casemix.py`；页面 **病例组合基准** |
| 分层器：可比病例组合、覆盖诊断、共同支持诊断 | `services/casemix.py::stratify`；未覆盖病例与单机构组合直接登记门禁 |
| 期望值计算器：直接标准化与相似病例匹配两条路线，输出标准服务量/费用区间与观察残差 | `services/casemix.py::direct_standardization / matching_standardization`；残差仅排优先级，非违法结论 |
| 门禁：未覆盖/共同支持失败病例禁止外推为可疑；小组病例 t 分位加宽区间且不判可疑 | `services/casemix.py::gate_decisions`；验收 `casemix_common_support_gate`、`casemix_small_group_interval` |
| 虚构网络四类验收原型 | 见下节，**总览页“一键验收”** 或 `GET /api/acceptance/run`（13 项断言） |

### 虚构验收网络（全部为虚构数据）

- **P1 / C-001（多点重复购药，成立支持链）**：伪标识 `P1`（含药店建档镜像 `P1B`，强簇）
  在 **康宁（朝阳店）PH1** 与 **百草堂（东城店）PH2** 同日（2026-03-10）取得同成分 **氨氯地平**，
  数量 30/28、给药期重叠 28 天，两笔结算互相落在同一给药期间；开方医师陈明 `D1` 当日排班 **停诊 SCH1**
  且无多点执业备案。规则与图模型给出完全相同的核心支持链，在“多点重复购药”同目标开放线索中均排第一。
- **P2/P3 / C-002（弱身份，不并人）**：共用养老机构电话 `010-88886666` 与代办社工（两条弱证据），
  但身份簇始终分离；各自处方、本人签收 `DL1/DL2`、养老床位 `BED2/BED3` 均匹配。弱边不可继续扩展。
- **P4 / C-003（合法供药）**：表面同日重复购药由药店间调拨 `TR4`（PH4→PH3，批号一致）、
  进销存出入库闭环与串户结算全额红冲更正 `SRC-ST5B-C2` 解释；重叠边置 `negated`，不再进入扩展。
- **P5 / C-004（反证关闭）**：申诉 `APP1` 成立，退货验收单 `TH-0318` 与 `return_in` 台账印证药品退回，
  结算全额退款，案件 `closed_returned`。
- 另有：**C-005** P9 同诊三张处方（拆方）、**C-006** PH5 同终端 60 分钟四人集中结算、
  **C-007** P14 90 日三次同向转诊 + ¥88,200 结算（高金额跨机构扩展）、
  **C-008** P6 慢病协作月度续方（给药期不重叠 + 长处方备案，不命中并排除）。

### 病例组合基准样例（全部虚构，页面 **病例组合基准**）

- **两家医院各 100 次就诊**：H6 康平区人民医院重症占比 **60%**（60 重症/40 普通）、
  H7 安宁区人民医院重症占比 **20%**（20 重症/80 普通）；重症组单例费用 **8000 元**、
  普通组 **3000 元**（组内加与匹配特征无关的成对噪声，组均值严格保持 8000/3000）。
- 每个 episode 由 3~4 张**结算行汇总**（手术麻醉/床位护理/检查检验/药品耗材等），
  携带入院诊断、年龄、急诊/住院类型、手术复杂度与生效政策 `POL-CASEMIX@v2026.1`。
- **未标准化对照**：核心 100 例口径下 H6 人均 6000、H7 人均 4000，表面差 **2000 元**，
  差异完全来自重症比例而非同组费用水平；两条标准化路线下两机构**期望≈观察、残差≈0**。
- **两条路线估计同一口径的期望费用残差**：
  - 路线 A **预登记临床分组直接标准化**（CG-SEV/CG-ROU/CG-RARE）：解释稳定可复算，
    细分不足时组内差异保留在残差中；
  - 路线 B **相似病例匹配**：候选邻居限定同预登记组（**不改变组别口径**），按诊断/年龄/
    就诊类型/手术复杂度做 Gower 距离近邻匹配，区分更细但依赖共同支持。
- **门禁与小组**：H6 独有的罕见联合手术组 CG-RARE（4 例，H7 为 0）无跨机构共同支持、
  H7 独有未入组的 Z00 体检例，两条路线都**拒绝外推、不判可疑**；H7 重症组 20 例低于
  小组阈值 30，区间按 **t 分位加宽**且只观察不判可疑。所有残差均**不作为违法结论**。

---

## 2. 一键 Docker 启动（推荐验收方式）

```bash
cd /workspace/85-feature-2
docker compose up -d --build
# 等待 app 健康（首次会自动建表并播种虚构网络）
docker compose ps
```

- 平台：<http://localhost:8000>
- Swagger / OpenAPI：<http://localhost:8000/docs>
- 数据库端口映射到宿主机 **5433**（账号/库/密码均为 `ccdev`），避免与本机 PostgreSQL 冲突。
- 停止：`docker compose down`；清空数据卷重来：`docker compose down -v`。

> 说明：本交付容器内未安装 docker，因此镜像构建未在开发容器中实际执行；
> Dockerfile 采用与已验证本地流程完全一致的 Node 22 构建 + Python 3.12 运行步骤。
> 下列“本地开发与验证”均为在本容器预装运行时上**实际执行并通过**的结果。

---

## 3. 本地开发与验证（已实际执行）

### 3.1 数据库（容器内预装 PostgreSQL 18）

```bash
# 已验证 ccdev 可连；创建独立库（只需一次）
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d ccdev \
  -c "CREATE DATABASE riskgraph;"
```

### 3.2 后端（Python 3.12 + venv）

```bash
cd /workspace/85-feature-2/backend
/opt/python-3.12/bin/python3 -m venv /workspace/85-feature-2/.venv
/workspace/85-feature-2/.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -r requirements.txt

# 初始化/重置播种（也可跳过：API 启动时空库会自动播种）
/workspace/85-feature-2/.venv/bin/python -m app.seed

# 启动（本地开发端口与 vite proxy 对齐为 8000）
DATABASE_URL='postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/riskgraph' \
STATIC_DIR=/workspace/85-feature-2/frontend/dist \
/workspace/85-feature-2/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 健康检查：<http://127.0.0.1:8000/api/health>
- Swagger：<http://127.0.0.1:8000/docs>

### 3.3 前端（Node 22）

```bash
cd /workspace/85-feature-2/frontend
/opt/node-22/bin/npm install --registry=https://registry.npmmirror.com

# 方式 A：开发服务器（/api 代理到 127.0.0.1:8000，可用 API_TARGET 覆盖）
/opt/node-22/bin/npm run dev        # http://localhost:5173

# 方式 B：构建后由 FastAPI 同源托管（上面的 STATIC_DIR 指向 dist）
/opt/node-22/bin/npm run build
```

### 3.4 测试账号

| 账号 | 密码 | 权限 |
| --- | --- | --- |
| `admin` | `admin123` | 管理员，全部统筹区/机构；可重置数据、重训模型、触发变更 |
| `auditor` | `audit123` | 朝阳片区稽核员（`region_scope=["CY"]`），仅可见 C-001/C-004/C-006，访问 C-002 详情返回 403；病例组合仅可见 CY 的 H6，访问 H7 episode 返回 403，可验证权限收敛 |

---

## 4. 本题验证方式与已执行结论

### 4.1 一键验收（13 项断言，全部通过）

- 界面：**总览 / 验收剧本 → 运行验收**（或登录后 `GET /api/acceptance/run`）。
- 命令行实测：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s http://127.0.0.1:8000/api/acceptance/run -H "Authorization: Bearer $TOKEN"
```

开发过程中的真实执行结果（服务层与 HTTP 层均跑过）：

```
OVERALL True 13 / 13
PASS source_ledger            # 308 条来源、0 缺引用、SRC-ST5B 更正链
PASS identity_grading         # P1/P1B 同强簇；P2/P3 异簇，IE-03 pending
PASS p1_chain                 # 同日/重叠28/同一给药期/停诊 + 7 条核心边
PASS same_chain_and_rank      # trace_chain_uids == support_chain；规则/模型同目标均 C-001 第一
PASS p23_weak_no_expand       # P3 为 terminal，ST4/RX4 不可达
PASS p4_transfer              # TR4 checked_explains，overlap negated 且扩展不可见
PASS p5_appeal_return         # APP1 upheld/returned，C-004 closed_returned
PASS restricted_expansion     # 2 跳 / 180 天，SCH1/PH1/PH2 可达，无越窗边
PASS high_amount_gate         # C-001 被拒、C-007 授权扩展并可达 bed 节点
PASS snapshot_immutable       # 快照#1 四要素齐全，判断 v1@snap1 带免责声明
PASS casemix_raw_vs_standardized   # 原始人均 6000 vs 4000（差2000）；标准化后两机构残差≈0且在区间内
PASS casemix_common_support_gate   # CG-RARE 支持失败、EP-H7-101 未覆盖；两路线均排除且门禁登记
PASS casemix_small_group_interval  # t(4)>t(24)>1.96；小组 t 加宽、不判可疑；CG-RARE 不产生区间
```

### 4.2 已实际执行的其他验证

- `python -m app.seed` 成功：`135 persons, 308 source records, 237 nodes, 267 edges, 205 casemix episodes, snapshot #1`。
- 遍历全部 GET API 与 **8 个案件详情**均 200，投影子图 5~11 节点 / 6~18 边，每案 6 条反证核查。
- 病例组合 8 个接口（overview/stratify/gates/unstandardized/benchmark×2/episodes/明细）HTTP 实测均 **200**；
  结算行汇总与 episode 观察费用逐例一致（`aggregation_consistent=true`）。
- 权限：`auditor`（仅 CY 片区）病例组合基准只可见 H6，访问 H7 的 episode 明细返回 **403**；
  原始案件侧仍仅列出 `['C-001','C-004','C-006']`，访问 `GET /api/cases/C-002` 返回 **403**。
- 病例组合数值核验（实测）：未标准化核心口径 H6=¥6,000 / H7=¥4,000（差 ¥2,000）；
  直接标准化两机构残差均为 0.0 且观察值落在期望区间；匹配路线残差 H6=-49.2 / H7=+245.0，
  均在 95% 区间内；CG-RARE（4 例）与 EP-H7-101 在两条路线均被门禁排除。
- 变更流（实测）：驳回 IE-03 → 快照 #2/身份簇 v2/影响 C-002；
  政策换版 → 快照 #3/影响 C-008 并追加判断 v2；
  ST12 退费更正 → 快照 #4/影响 C-005 并追加判断 v2（v1 保留）；
  案件人工状态写入核查时间线。
- `npm run build` **构建成功**（Vue SFC 全部通过编译，含 CaseMix chunk，产物由 FastAPI `/` 托管，实测 200）。
- 重置接口曾出现“请求会话持锁 + drop_all”自死锁，已通过重建前先 `rollback()/close()` 修复并复测秒回。
- 容器内**没有 docker**，因此未执行 `docker compose build`；Dockerfile/Compose 仅做了 YAML 解析与逻辑核对，
  镜像构建步骤与已验证的本地 Node/Python 流程一致。建议在装有 Docker 的机器上执行第 2 节命令做最终确认。
- 当前容器未预装 Playwright/Chromium，本轮未做浏览器巡检；页面经 Vite 生产构建与接口联调验证，
  建议按 4.3 做一次人工 UI 走查。

### 4.3 建议的人工 UI 走查（约 5 分钟）

1. `admin/admin123` 登录 → 总览 → **运行验收**，应全绿。
2. **身份解析**：看到 IE-01/IE-02 强证据自动成簇、IE-03~06 弱候选；可对 IE-03 驳回，提示新快照与 C-002。
3. **受限图扩展**：起点 `P1` 扩展，点红/橙边查看 `basis` 与来源编号；切到 `P2` 可见黄色虚线圈 P3 且无法延伸；
   高金额面板对 C-001 拒绝、C-007 授权并可冻结。
4. **模式与排序**：MULTI_POINT 下规则/模型两种排序 C-001 均第一；进 C-001 详情核对事实、
   完全相同的支持链 UID、模型特征贡献与校准因子。
5. **反证联查**：切换 C-003/C-004 看绿色“合理解释/确认反证”，C-001 看红色“仍未排除”。
6. **案件投影**：提交核查状态；切换判断版本；用 `auditor/audit123` 重新登录验证只能看朝阳案件。
7. **快照复演**：查看 #1 冻结图与政策/身份版本；执行政策换版或 ST12 退费，查看新快照、Diff 与受影响案件，
   再回 C-005/C-008 对比 v1 与新判断。
8. **病例组合基准**：先看“未标准化人均差 2000”提示；分层器页确认 CG-RARE 支持失败、EP-H7-101 未覆盖及门禁；
   期望值页切换路线 A/B，核对两机构残差≈0、H7 重症组小组加宽且无“可疑”标记；打开任一 episode 查看结算行汇总。

---

## 5. 目录结构

```
/workspace/85-feature-2
├── Dockerfile                 # 多阶段：node22 构建 → python3.12 托管
├── docker-compose.yml        # db(postgres:16) + app，健康依赖
├── README.md
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py            # FastAPI、启动播种、静态托管
│       ├── config.py database.py models.py security.py acceptance.py
│       ├── seed.py seed_casemix.py   # 虚构验收网络 + 病例组合 205 例 episode
│       ├── routers/           # auth/sources/identity/graph/patterns/cases/
│       │                      # snapshots/tools/casemix
│       └── services/          # identity/graph_builder/patterns/counterevidence/
│                              # graphmodel/expansion/projection/snapshot/ranking/rejudge/
│                              # casemix（分层器/两条路线期望/残差/门禁）
└── frontend/
    ├── vite.config.js package.json
    └── src/
        ├── App.vue AppLayout.vue main.js router.js api.js store.js styles.css
        ├── components/GraphCanvas.vue   # 自研 SVG 力导向图（支持链高亮/弱边终端/否定边）
        └── pages/             # Login/Dashboard/Sources/Identity/Explorer/Patterns/
                               # CaseMix/Cases/CaseDetail/Counter/Snapshots
```

## 6. 主要 API 一览

```
POST /api/auth/login
GET  /api/meta/stats
GET  /api/acceptance/run
POST /api/meta/reset                         (admin)
GET  /api/sources?source_system=&record_type=&status=&q=
GET  /api/sources/{code}
GET  /api/identity/evidence | /clusters
POST /api/identity/evidence/{code}/adjudicate
GET  /api/graph/expand?start_type=beneficiary|claim&start_code=P1&max_hops=2&window_days=180
POST /api/graph/high-amount-extension
GET  /api/patterns/configs | /ranking?pattern_code=&sort=
POST /api/patterns/evaluate
GET  /api/patterns/model/latest ; POST /api/patterns/model/train
GET  /api/cases ; GET /api/cases/{code} ; GET /api/cases/{code}/judgments/{v}
POST /api/cases/{code}/review
GET  /api/counter...（反证数据内含在 /api/cases/{code}）
GET  /api/snapshots ; /{no} ; /diff?from_no=&to_no=
POST /api/snapshots/policy-change | /refund-correction
# —— 病例组合基准 ——
GET  /api/casemix/overview
GET  /api/casemix/stratify
GET  /api/casemix/gates
GET  /api/casemix/unstandardized
GET  /api/casemix/benchmark?method=direct|matching
GET  /api/casemix/episodes?org=&group=&covered=
GET  /api/casemix/episodes/{code}
```

## 7. 边界与后续可加强

- 图模型是**可审计教学实现**（纯 Python 逻辑回归 + 确定性演练样本），用于演示时间切分、校准与路径追溯，
  不是生产级反欺诈模型；新型协作模式召回较低是规则法的已知特性。
- 认证为库内不透明令牌的轻量实现，生产应对接统一身份/审计网关并补充 HTTPS、CSRF 与更细字段级权限。
- 病例组合基准是**可审计教学实现**：直接标准化用合并组参考均值 + t 分位区间，匹配路线用确定性
  Gower 距离近邻（无随机过程、可复算）；样本为虚构演练数据，未做 DRG/DIP 权重付费与置信区间自助法。
  期望残差仅反映“相同病例结构下的相对偏离”，不构成违法违规结论。
- 数据全为虚构；未使用 Redis（题目环境虽预装），无搜索/JSON 模块依赖。
