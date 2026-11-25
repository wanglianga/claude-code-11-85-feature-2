<template>
  <div class="page">
    <h2 class="page-title">病例组合基准（Case-Mix）与期望费用残差</h2>
    <p class="page-sub">
      结算行汇总到就诊 <b>episode</b>，关联入院诊断、年龄、急诊/住院类型、手术复杂度与生效政策；
      分层器先给出<b>可比病例组合与覆盖诊断</b>，期望值计算器在<b>相同病例结构</b>下估算每机构
      标准服务量/费用区间与观察值残差。门禁禁止把未覆盖或无共同支持的病例外推为可疑。
    </p>
    <div class="disclaimer-banner">
      ⚖️ {{ disclaimer || '期望残差仅用于排定核查优先级，不构成欺诈或违法违规结论；定性须经人工核查与法定程序。' }}
    </div>

    <!-- 总览指标 -->
    <div class="grid stat-grid mb8">
      <div class="stat"><div class="n">{{ overview.total_episodes ?? '—' }}</div><div class="l">就诊 episode 数</div></div>
      <div class="stat"><div class="n">{{ overview.total_claim_lines ?? '—' }}</div><div class="l">结算行（汇总到 episode）</div></div>
      <div class="stat"><div class="n">{{ coveragePct }}%</div><div class="l">预登记分组覆盖率</div></div>
      <div class="stat"><div class="n">{{ overview.groups?.length ?? '—' }}</div><div class="l">预登记临床分组</div></div>
    </div>

    <!-- 未标准化 vs 标准化 -->
    <div class="card">
      <h3>① 为什么不能单看人均费用：未标准化均值差 vs 标准化残差</h3>
      <el-table :data="rawRows" size="small" border>
        <el-table-column prop="org" label="机构" width="90" />
        <el-table-column label="重症占比" width="100">
          <template #default="{ row }">{{ row.severeShare }}%</template>
        </el-table-column>
        <el-table-column label="未标准化人均费用" width="170">
          <template #default="{ row }">
            <b :class="row.fakeHigh ? 'fact-pass' : ''">{{ yuan(row.rawMean) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="直接标准化后期望/观察" min-width="220">
          <template #default="{ row }">
            <span v-if="stdMap[row.org]">
              观察 {{ yuan(stdMap[row.org].observed.total_amount) }} ／
              期望 {{ yuan(stdMap[row.org].expected.total_amount) }}
              <el-tag size="small" type="success" effect="plain" style="margin-left:6px">
                残差 {{ yuan(stdMap[row.org].residual.total_amount) }}
              </el-tag>
            </span>
          </template>
        </el-table-column>
      </el-table>
      <p class="small muted mt8" style="line-height:1.8">
        核心 100 例口径：H6 人均 {{ yuan(raw?.core?.by_org?.H6?.mean_amount) }}、
        H7 人均 {{ yuan(raw?.core?.by_org?.H7?.mean_amount) }}，表面相差
        <b class="fact-pass">{{ yuan(raw?.core?.mean_diff) }}</b>，
        差异完全来自重症比例（60% vs 20%）；按相同病例结构标准化后两机构残差均≈0。
        {{ raw?.interpretation }}
      </p>
    </div>

    <el-tabs v-model="tab" class="card" @tab-change="onTab">
      <!-- 分层与门禁 -->
      <el-tab-pane label="② 分层器 · 覆盖与门禁" name="strata">
        <h3>可比病例组合（按预登记临床分组）</h3>
        <el-table :data="strata.groups || []" size="small" border>
          <el-table-column prop="group_code" label="分组编码" width="110" />
          <el-table-column prop="group_name" label="分组名称" min-width="150" />
          <el-table-column label="各机构病例数" min-width="150">
            <template #default="{ row }">
              <el-tag v-for="(n, o) in row.counts_by_org" :key="o" size="small"
                      :type="n === 0 ? 'danger' : (n < row.min_group_n ? 'warning' : 'success')"
                      effect="plain" style="margin-right:6px">{{ o }}: {{ n }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="覆盖诊断" min-width="220">
            <template #default="{ row }">
              <span class="small">{{ row.covered_diagnoses.join('；') }}</span>
            </template>
          </el-table-column>
          <el-table-column label="共同支持 / 门禁" width="200">
            <template #default="{ row }">
              <el-tag v-if="row.common_support" type="success" size="small">共同支持成立</el-tag>
              <el-tag v-else type="danger" size="small">支持失败·禁止外推</el-tag>
              <el-tag v-if="row.small_orgs.length" type="warning" size="small" style="margin-left:4px">
                小组 {{ row.small_orgs.join(',') }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <el-row :gutter="12" class="mt12">
          <el-col :span="12">
            <h3>门禁结论（未覆盖 / 共同支持失败 / 小组）</h3>
            <el-table :data="gates" size="small" border max-height="320">
              <el-table-column prop="gate" label="门禁类型" width="120">
                <template #default="{ row }">
                  <el-tag size="small" :type="gateType(row.gate)">{{ gateLabel(row.gate) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="scope" label="对象" width="120" />
              <el-table-column prop="reason" label="原因与处置" min-width="220">
                <template #default="{ row }"><span class="small">{{ row.reason }}；{{ row.action }}</span></template>
              </el-table-column>
            </el-table>
          </el-col>
          <el-col :span="12">
            <h3>未覆盖病例（不入组、不外推）</h3>
            <el-table :data="strata.uncovered || []" size="small" border max-height="320">
              <el-table-column prop="episode" label="Episode" width="120" />
              <el-table-column prop="org" label="机构" width="80" />
              <el-table-column prop="diagnosis" label="入院诊断" min-width="140" />
              <el-table-column label="处置" width="110">
                <template #default><el-tag size="small" type="danger">禁止外推</el-tag></template>
              </el-table-column>
            </el-table>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- 两条标准化路线 -->
      <el-tab-pane label="③ 期望值计算器 · 两条路线" name="bench">
        <div class="flex-between mb8">
          <el-radio-group v-model="method" @change="loadBenchmark">
            <el-radio-button value="direct">路线 A：预登记分组直接标准化（解释稳定）</el-radio-button>
            <el-radio-button value="matching">路线 B：相似病例匹配（区分更细·依赖共同支持）</el-radio-button>
          </el-radio-group>
        </div>
        <p class="small muted" style="line-height:1.7">
          {{ bench?.method_note }}
          <template v-if="method === 'matching'">
            近邻参数 k={{ bench?.params?.k }}，最少邻居 {{ bench?.params?.min_neighbors }}，
            距离阈值 {{ bench?.params?.distance_threshold }}；候选邻居限定同预登记组，<b>不改变组别口径</b>。
          </template>
        </p>

        <el-table :data="bench?.orgs || []" size="small" border class="mt8">
          <el-table-column prop="org_code" label="机构" width="80" fixed />
          <el-table-column label="基准/排除病例" width="130">
            <template #default="{ row }">
              <div class="small">纳入 {{ row.benchmark_episodes }} / 总 {{ row.episodes_total }}</div>
              <el-tooltip v-if="row.excluded_episodes.length"
                          :title="row.excluded_episodes.map((x) => `${x.code}：${x.reason}`).join('\n')"
                          placement="top">
                <el-tag size="small" type="danger" style="margin-top:2px">
                  门禁排除 {{ row.excluded_episodes.length }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="观察费用" width="130">
            <template #default="{ row }">
              <b>{{ yuan(row.observed.total_amount) }}</b>
              <div class="small muted">人均 {{ yuan(row.observed.mean_amount) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="期望费用区间（95%）" min-width="210">
            <template #default="{ row }">
              <div>{{ yuan(row.expected.total_low) }} ~ {{ yuan(row.expected.total_high) }}</div>
              <div class="small muted">点估计 {{ yuan(row.expected.total_amount) }} ／
                人均 {{ yuan(row.expected.mean_low) }}~{{ yuan(row.expected.mean_high) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="标准服务量区间" min-width="170">
            <template #default="{ row }">
              <div class="small">{{ row.expected.units_low }} ~ {{ row.expected.units_high }} 单元</div>
              <div class="small muted">观察 {{ row.observed.total_units }} 单元</div>
            </template>
          </el-table-column>
          <el-table-column label="观察残差" width="150">
            <template #default="{ row }">
              <b :class="row.residual.within_interval ? 'fact-fail' : 'fact-pass'">
                {{ yuan(row.residual.total_amount) }}
              </b>
              <div class="small muted">({{ row.residual.pct }}%)</div>
            </template>
          </el-table-column>
          <el-table-column label="判定（非结论）" min-width="180">
            <template #default="{ row }">
              <el-tag size="small" :type="flagType(row.residual.flag)">{{ flagLabel(row.residual.flag) }}</el-tag>
              <div class="small muted" style="line-height:1.5">{{ row.residual.flag_note }}</div>
            </template>
          </el-table-column>
        </el-table>

        <el-collapse class="mt12">
          <el-collapse-item v-for="row in (bench?.orgs || [])" :key="row.org_code"
                            :title="`${row.org_code} · 分组明细（含小组加宽）`" :name="row.org_code">
            <el-table :data="row.group_breakdown" size="small" border>
              <el-table-column prop="group_code" label="分组" width="110" />
              <el-table-column prop="group_name" label="名称" min-width="130" />
              <el-table-column prop="cases" label="病例数" width="80" />
              <el-table-column v-if="method === 'direct'" prop="reference_mean" label="组参考均值" width="110">
                <template #default="{ row }">{{ yuan(row.reference_mean) }}</template>
              </el-table-column>
              <el-table-column label="观察组均值" width="110">
                <template #default="{ row }">{{ yuan(row.observed_mean) }}</template>
              </el-table-column>
              <el-table-column v-if="method === 'direct'" label="组期望总量区间" min-width="190">
                <template #default="{ row }">
                  <span class="small">{{ yuan(row.expected_total_low) }} ~ {{ yuan(row.expected_total_high) }}</span>
                </template>
              </el-table-column>
              <el-table-column v-else prop="matched_expected_mean" label="匹配期望均值" width="120">
                <template #default="{ row }">{{ yuan(row.matched_expected_mean) }}</template>
              </el-table-column>
              <el-table-column label="小组处理" width="120">
                <template #default="{ row }">
                  <el-tag v-if="row.small_group" size="small" type="warning">小组·t 加宽</el-tag>
                  <el-tag v-else size="small" type="success">样本充足</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="method === 'matching' && row.match_examples?.length" class="mt8">
              <b class="small">匹配样例（邻居来自其他机构同组病例）：</b>
              <el-table :data="row.match_examples" size="small" border class="mt8">
                <el-table-column prop="episode" label="Episode" width="120" />
                <el-table-column prop="diagnosis" label="诊断" min-width="140" />
                <el-table-column prop="neighbors" label="近邻数" width="80" />
                <el-table-column prop="max_distance" label="最大 Gower 距离" width="130" />
                <el-table-column label="邻居编码" min-width="200">
                  <template #default="{ row }"><span class="chain-uid">{{ row.neighbor_codes.join(' / ') }}</span></template>
                </el-table-column>
              </el-table>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <!-- Episode 明细 -->
      <el-tab-pane label="④ Episode 与结算行明细" name="eps">
        <div class="flex gap8 mb8">
          <el-select v-model="filter.org" placeholder="机构" clearable style="width:140px" @change="loadEpisodes">
            <el-option v-for="o in strata.orgs || []" :key="o" :label="o" :value="o" />
          </el-select>
          <el-select v-model="filter.group" placeholder="临床分组" clearable style="width:180px" @change="loadEpisodes">
            <el-option v-for="g in overview.groups || []" :key="g.code" :label="`${g.code} ${g.name}`" :value="g.code" />
          </el-select>
          <el-select v-model="filter.covered" placeholder="覆盖状态" clearable style="width:150px" @change="loadEpisodes">
            <el-option label="已覆盖" :value="true" />
            <el-option label="未覆盖" :value="false" />
          </el-select>
          <el-tag type="info" effect="plain">共 {{ episodes.total }} 条</el-tag>
        </div>
        <el-table :data="episodes.items" size="small" border height="460"
                  @row-click="openEpisode" highlight-current-row>
          <el-table-column prop="code" label="Episode" width="120" />
          <el-table-column prop="org_code" label="机构" width="80" />
          <el-table-column prop="diagnosis" label="入院诊断" min-width="160" />
          <el-table-column prop="age" label="年龄" width="70" />
          <el-table-column prop="visit_kind" label="类型" width="90" />
          <el-table-column prop="surgery_level" label="手术复杂度" width="110" />
          <el-table-column prop="clinical_group_code" label="临床分组" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.clinical_group_code" size="small">{{ row.clinical_group_code }}</el-tag>
              <el-tag v-else size="small" type="danger">未覆盖</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="policy" label="生效政策" width="150" />
          <el-table-column prop="line_count" label="结算行数" width="90" />
          <el-table-column label="观察费用" width="120">
            <template #default="{ row }"><b>{{ yuan(row.observed_amount) }}</b></template>
          </el-table-column>
          <el-table-column label="样本块" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.sample_block === 'core' ? 'success' : 'warning'">
                {{ row.sample_block === 'core' ? '核心样本' : '支持失败例' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 结算行明细抽屉 -->
    <el-drawer v-model="drawer" size="640px" title="Episode 结算行汇总">
      <template v-if="detail">
        <div class="kv mb8">
          <div class="k">Episode</div><div>{{ detail.episode.code }}</div>
          <div class="k">机构 / 参保人</div><div>{{ detail.episode.org_code }} / {{ detail.episode.person_code }}</div>
          <div class="k">入院诊断</div><div>{{ detail.episode.diagnosis }}（{{ detail.episode.age }} 岁）</div>
          <div class="k">类型 / 手术</div><div>{{ detail.episode.visit_kind }} / {{ detail.episode.surgery_level }}</div>
          <div class="k">临床分组</div>
          <div>{{ detail.episode.clinical_group_code || '未覆盖（门禁禁止外推）' }}</div>
          <div class="k">生效政策</div><div>{{ detail.episode.policy_code }}@{{ detail.episode.policy_version }}</div>
        </div>
        <el-table :data="detail.claim_lines" size="small" border>
          <el-table-column prop="line_no" label="结算行号" width="130" />
          <el-table-column prop="category" label="费用类别" width="100" />
          <el-table-column prop="fee_item" label="费用项" min-width="120" />
          <el-table-column prop="units" label="服务单元" width="90" />
          <el-table-column label="金额" width="110">
            <template #default="{ row }">{{ yuan(row.amount) }}</template>
          </el-table-column>
        </el-table>
        <div class="flex-between mt12">
          <el-tag :type="detail.aggregation_consistent ? 'success' : 'danger'">
            行汇总 {{ yuan(detail.line_sum) }} {{ detail.aggregation_consistent ? '=' : '≠' }}
            episode 观察费用 {{ yuan(detail.episode.observed_amount) }}
          </el-tag>
          <span class="small muted">共 {{ detail.claim_lines.length }} 行 / {{ detail.episode.service_units }} 服务单元</span>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../api.js'
import { yuan } from '../store.js'

const overview = ref({})
const strata = ref({})
const gates = ref([])
const raw = ref(null)
const bench = ref(null)
const method = ref('direct')
const tab = ref('strata')
const episodes = ref({ items: [], total: 0 })
const filter = reactive({ org: null, group: null, covered: null })
const drawer = ref(false)
const detail = ref(null)

const disclaimer = computed(() => bench.value?.disclaimer)
const coveragePct = computed(() => Math.round((overview.value.coverage_rate || 0) * 100))
const stdMap = computed(() => Object.fromEntries((bench.value?.orgs || []).map((r) => [r.org_code, r])))
const rawRows = computed(() => {
  const core = raw.value?.core?.by_org || {}
  const severe = { H6: 60, H7: 20 }
  return Object.keys(core).sort().map((org) => ({
    org, rawMean: core[org].mean_amount, severeShare: severe[org] ?? '—',
    fakeHigh: org === 'H6'
  }))
})

const GATE_META = {
  common_support: ['danger', '共同支持失败'],
  coverage: ['danger', '未覆盖'],
  small_group: ['warning', '小组加宽']
}
const gateType = (g) => GATE_META[g]?.[0] || 'info'
const gateLabel = (g) => GATE_META[g]?.[1] || g
const FLAG_META = {
  within_benchmark: ['success', '区间内'],
  small_group_widened: ['warning', '小组加宽·不判可疑'],
  out_of_interval_review: ['danger', '超区间·建议人工核查']
}
const flagType = (f) => FLAG_META[f]?.[0] || 'info'
const flagLabel = (f) => FLAG_META[f]?.[1] || f

async function loadBenchmark() {
  bench.value = await api.get(`/api/casemix/benchmark?method=${method.value}`)
}
async function loadEpisodes() {
  const p = new URLSearchParams()
  if (filter.org) p.set('org', filter.org)
  if (filter.group) p.set('group', filter.group)
  if (filter.covered !== null) p.set('covered', filter.covered)
  episodes.value = await api.get(`/api/casemix/episodes?${p.toString()}`)
}
async function openEpisode(row) {
  detail.value = await api.get(`/api/casemix/episodes/${row.code}`)
  drawer.value = true
}
function onTab(name) {
  if (name === 'bench' && !bench.value) loadBenchmark()
  if (name === 'eps' && !episodes.value.items.length) loadEpisodes()
}

onMounted(async () => {
  const [ov, st, ga, un] = await Promise.all([
    api.get('/api/casemix/overview'),
    api.get('/api/casemix/stratify'),
    api.get('/api/casemix/gates'),
    api.get('/api/casemix/unstandardized')
  ])
  overview.value = ov
  strata.value = st
  gates.value = ga.items
  raw.value = un
  await loadBenchmark()
})
</script>
