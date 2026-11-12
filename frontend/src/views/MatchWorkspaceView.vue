<template>
  <div>
    <div class="toolbar">
      <select v-model="batchId" style="width:260px" @change="loadWorkspace">
        <option v-for="b in batches" :key="b.id" :value="b.id">
          {{ b.batch_ref }} v{{ b.version }} · {{ b.channel }} · {{ b.status }}
        </option>
      </select>
      <button class="primary" :disabled="!batchId" @click="runAuto">运行自动对账（仅生成建议）</button>
      <button :disabled="!batchId" @click="loadWorkspace">刷新</button>
      <div class="spacer"></div>
      <label style="display:flex;align-items:center;gap:6px;margin:0">
        <span class="muted">操作人</span>
        <input v-model="operator" style="width:150px" />
      </label>
    </div>

    <div v-if="!ws" class="card empty">请选择结算批次</div>

    <template v-if="ws">
      <!-- 批次头 -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap">
          <div>
            <div class="card-title" style="margin-bottom:6px">
              {{ ws.batch.batch_ref }} v{{ ws.batch.version }}
              <span class="badge" :class="ws.batch.status" style="margin-left:6px">{{ ws.batch.status }}</span>
            </div>
            <div class="muted" style="font-size:12.5px">
              渠道 {{ ws.batch.channel }} · 结算主体 <b>{{ ws.batch.legal_entity }}</b> ·
              周期 {{ ws.batch.period_start.slice(0, 10) }} ~ {{ ws.batch.period_end.slice(0, 10) }} ·
              币种 {{ ws.batch.currency }}
            </div>
          </div>
          <div class="grid cols-3" style="gap:24px">
            <div style="text-align:right">
              <div class="muted" style="font-size:12px">总额</div>
              <div class="stat-value" style="font-size:18px">{{ fmt(ws.batch.gross_amount) }}</div>
            </div>
            <div style="text-align:right">
              <div class="muted" style="font-size:12px">手续费/保证金/调整</div>
              <div class="stat-value" style="font-size:18px">{{ fmt(ws.batch.fee_amount) }}</div>
            </div>
            <div style="text-align:right">
              <div class="muted" style="font-size:12px">净额</div>
              <div class="stat-value" style="font-size:18px">{{ fmt(ws.batch.net_amount) }}</div>
            </div>
          </div>
        </div>
        <div class="pill-row" style="margin-top:14px">
          <span class="completion-tag" :class="ws.level1.complete ? 'ok' : 'warn'">
            LEVEL 1 订单→结算行：{{ ws.level1.confirmed_count }}/{{ ws.level1.order_line_count }} 行已人工确认
            {{ ws.level1.complete ? '· 逐笔完成' : '· 未完成' }}
          </span>
          <span class="completion-tag" :class="ws.level2.complete ? 'ok' : 'warn'">
            LEVEL 2 结算行→银行流水：已确认 {{ fmt(ws.level2.confirmed_total) }} /
            有效分配 {{ fmt(ws.level2.active_total) }} / 批次净额 {{ fmt(ws.batch.net_amount) }}
            {{ ws.level2.complete ? '· 逐笔完成' : '· 未完成' }}
          </span>
        </div>
        <div v-if="ws.level2.totals_equal_but_incomplete" class="warn-banner">
          ⚠ 总额相等陷阱：银行入账合计 {{ fmt(ws.level2.bank_inflow_total) }}
          与批次净额 {{ fmt(ws.batch.net_amount) }} 相等，但逐笔匹配<b>未完成</b>
          （存在 PROPOSED 建议、跨主体流水或未结转余额）。系统不会因总额相等而标记逐笔匹配完成。
        </div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">{{ ws.level2.completion_rule }}</p>
      </div>

      <!-- ============ LEVEL 1 ============ -->
      <div class="card section-gap">
        <div class="card-title">
          LEVEL 1 · 订单业务事件 → 渠道结算行
          <span class="muted" style="font-weight:400">（自动匹配仅为建议，人工确认后留痕依据与置信度）</span>
        </div>
        <div class="table-wrap">
          <table class="data">
            <thead>
              <tr>
                <th>结算行</th><th>类型</th><th>订单/渠道参考</th>
                <th class="num">总额</th><th class="num">手续费</th><th class="num">净额</th>
                <th>建议匹配 / 依据 / 置信度</th><th>状态</th><th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in ws.level1.rows" :key="r.line_id" :class="{ 'component-row': r.is_non_order_line }">
                <td class="mono">{{ r.line_ref }}</td>
                <td>
                  <span class="tag" :class="r.is_non_order_line ? 'tag-component' : ''">{{ lineTypeLabel(r.line_type) }}</span>
                </td>
                <td class="mono">{{ r.order_ref || '—' }}<div class="muted" style="font-size:11px">{{ r.channel_ref || '' }}</div></td>
                <td class="num">{{ fmt(r.gross_amount) }}</td>
                <td class="num">{{ fmt(r.fee_amount) }}</td>
                <td class="num"><b>{{ fmt(r.net_amount) }}</b></td>
                <td v-if="r.is_non_order_line">
                  <span class="muted">非订单成分行（{{ componentLabel(r.component_kind) }}），不参与订单匹配</span>
                </td>
                <td v-else>
                  <div v-for="m in r.matches" :key="m.id" class="match-cell">
                    <span class="mono">{{ m.event_ref || m.business_event_id }}</span>
                    <span class="tag tag-basis">{{ basisLabel(m.match_basis) }}</span>
                    <span class="conf" :class="confClass(m.confidence)">{{ m.confidence || '—' }}</span>
                    <a class="link" @click="toggleDetail(`l1-${m.id}`)">依据</a>
                    <pre v-if="openDetail[`l1-${m.id}`]" class="json">{{ JSON.stringify(m.match_detail, null, 2) }}</pre>
                  </div>
                  <span v-if="!r.matches.length" class="neg">无建议（引擎未找到订单事实）</span>
                </td>
                <td>
                  <span v-if="r.is_non_order_line" class="tag tag-component">{{ componentLabel(r.component_kind) }}</span>
                  <span v-else-if="!r.active_match_count" class="badge OPEN">未匹配</span>
                  <span v-else class="badge" :class="r.fully_confirmed ? 'CONFIRMED' : 'PROPOSED'">
                    {{ r.fully_confirmed ? '已确认' : '待确认' }}
                  </span>
                </td>
                <td>
                  <div v-if="!r.is_non_order_line" class="pill-row">
                    <button v-for="m in r.matches.filter((x) => x.status === 'PROPOSED')" :key="m.id"
                            class="sm primary" @click="confirmL1(m)">确认</button>
                    <button v-for="m in r.matches.filter((x) => x.status === 'PROPOSED')" :key="`r${m.id}`"
                            class="sm danger" @click="reject(m)">拒收</button>
                    <button class="sm" @click="openManualL1(r)">手工选择</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- ============ LEVEL 2 ============ -->
      <div class="card section-gap">
        <div class="card-title">
          LEVEL 2 · 结算行 → 银行流水（合并 / 拆分 / 部分到账 / 跨主体）
        </div>

        <div class="grid cols-2" style="gap:18px">
          <div>
            <div class="soft" style="font-weight:600;margin-bottom:8px">银行流水（含未解释残差与收款主体）</div>
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>流水号</th><th>收款主体</th><th>入账日</th><th class="num">金额</th><th class="num">已分配</th><th class="num">残差</th></tr></thead>
                <tbody>
                  <tr v-for="b in ws.level2.banks" :key="b.bank_transaction_id" :class="{ 'cross-row': b.cross_entity }">
                    <td class="mono">{{ b.bank_ref }}<div class="muted" style="font-size:11px">{{ b.memo }}</div></td>
                    <td>
                      {{ b.legal_entity }}
                      <span v-if="b.cross_entity" class="tag tag-cross">跨主体</span>
                    </td>
                    <td>{{ b.booked_at.slice(0, 10) }}</td>
                    <td class="num">{{ fmt(b.amount) }}</td>
                    <td class="num">{{ fmt(b.allocated_amount) }}</td>
                    <td class="num" :class="Number(b.residual_amount) !== 0 ? 'neg' : ''">{{ fmt(b.residual_amount) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div>
            <div class="soft" style="font-weight:600;margin-bottom:8px">结算行覆盖进度（逐笔）</div>
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>行</th><th>类型</th><th class="num">净额</th><th class="num">已分配</th><th class="num">未匹配余额</th><th>状态</th></tr></thead>
                <tbody>
                  <tr v-for="l in ws.level2.lines" :key="l.line_id">
                    <td class="mono">{{ l.line_ref }}</td>
                    <td><span class="tag" :class="l.is_non_order_line ? 'tag-component' : ''">{{ lineTypeLabel(l.line_type) }}</span></td>
                    <td class="num">{{ fmt(l.net_amount) }}</td>
                    <td class="num">{{ fmt(l.allocated_amount) }}</td>
                    <td class="num" :class="Number(l.remaining_amount) !== 0 ? 'neg' : ''">
                      {{ l.is_netted_line ? '轧差结算' : fmt(l.remaining_amount) }}
                    </td>
                    <td>
                      <span class="badge" :class="l.complete ? 'CONFIRMED' : 'OPEN'">
                        {{ l.complete ? '逐笔完成' : '待匹配' }}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="soft" style="font-weight:600;margin:16px 0 8px">匹配关系（自动建议 / 人工拆分，含依据与置信度）</div>
        <div class="table-wrap">
          <table class="data">
            <thead>
              <tr><th>#</th><th>结算行</th><th>银行流水</th><th class="num">分配金额</th><th>拆分</th><th>依据</th><th>置信度</th><th>状态</th><th>确认人</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="m in ws.level2.matches" :key="m.id">
                <td class="mono muted">{{ m.id }}<span v-if="m.parent_match_id" class="tag" style="margin-left:4px">子分配</span></td>
                <td class="mono">{{ m.line_ref || '批次级建议' }}</td>
                <td class="mono">
                  {{ m.bank_ref || m.bank_transaction_id }}
                  <span v-if="m.bank_legal_entity && m.bank_legal_entity !== ws.batch.legal_entity" class="tag tag-cross">跨主体</span>
                </td>
                <td class="num">{{ fmt(m.allocated_amount) }} {{ m.currency }}</td>
                <td><span class="tag">{{ splitLabel(m.split_rule) }}</span></td>
                <td><span class="tag tag-basis">{{ basisLabel(m.match_basis) }}</span>
                  <a class="link" @click="toggleDetail(`l2-${m.id}`)">详情</a>
                  <pre v-if="openDetail[`l2-${m.id}`]" class="json">{{ JSON.stringify(m.match_detail, null, 2) }}</pre>
                </td>
                <td><span class="conf" :class="confClass(m.confidence)">{{ m.confidence || '—' }}</span></td>
                <td><span class="badge" :class="m.status">{{ statusLabel(m.status) }}</span></td>
                <td>{{ m.confirmed_by || '—' }}</td>
                <td>
                  <div class="pill-row">
                    <button v-if="m.status === 'PROPOSED'" class="sm primary" @click="confirmL2(m)">人工确认</button>
                    <button v-if="m.status === 'PROPOSED'" class="sm danger" @click="reject(m)">拒收</button>
                  </div>
                </td>
              </tr>
              <tr v-if="!ws.level2.matches.length"><td colspan="10" class="empty">暂无 LEVEL2 匹配关系</td></tr>
            </tbody>
          </table>
        </div>

        <div style="margin-top:14px">
          <button class="primary" @click="openSplit">手工拆分挂账（比例 / 指定明细）</button>
        </div>
      </div>

      <!-- ============ 结转挂账 ============ -->
      <div class="card section-gap">
        <div class="card-title">未匹配余额结转挂账（单独结转，不混入其他流水凑平）</div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>#</th><th>结转原因</th><th>来源</th><th class="num">金额</th><th>状态</th><th>登记人</th><th>明细</th></tr></thead>
            <tbody>
              <tr v-for="c in ws.carry_forwards" :key="c.id">
                <td class="mono muted">{{ c.id }}</td>
                <td><span class="tag" :class="c.reason === 'CROSS_ENTITY' ? 'tag-cross' : 'tag-component'">{{ reasonLabel(c.reason) }}</span></td>
                <td class="mono">{{ c.bank_ref ? `流水 ${c.bank_ref}` : '' }}{{ c.line_ref ? `结算行 ${c.line_ref}` : '' }}</td>
                <td class="num">{{ fmt(c.amount) }} {{ c.currency }}</td>
                <td><span class="badge" :class="c.status === 'OPEN' ? 'OPEN' : 'CLOSED'">{{ c.status === 'OPEN' ? '挂账中' : '已结清' }}</span></td>
                <td>{{ c.created_by }}</td>
                <td class="muted" style="font-size:12px">{{ JSON.stringify(c.detail) }}</td>
              </tr>
              <tr v-if="!ws.carry_forwards.length"><td colspan="7" class="empty">暂无结转挂账</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- LEVEL1 手工选择弹窗 -->
    <div v-if="manualL1" class="modal-mask" @click.self="manualL1 = null">
      <div class="modal" style="width:640px">
        <h3>手工建立 LEVEL1 匹配 · 结算行 {{ manualL1.line_ref }}</h3>
        <p class="muted" style="font-size:12.5px">选择对应的订单业务事件，确认后置信度记 100，依据记为人工选择。</p>
        <div class="table-wrap" style="max-height:340px;overflow-y:auto">
          <table class="data">
            <thead><tr><th></th><th>事件</th><th>类型</th><th>订单</th><th>渠道参考</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="e in candidateEvents" :key="e.id">
                <td><input type="radio" name="evt" :value="e.id" v-model="l1Form.event_id" style="width:auto" /></td>
                <td class="mono">{{ e.entity_ref }}</td>
                <td>{{ e.event_type }}</td>
                <td class="mono">{{ e.order_ref || '—' }}</td>
                <td class="mono">{{ e.channel_ref || '—' }}</td>
                <td>{{ e.event_time.slice(0, 16).replace('T', ' ') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <label class="field" style="margin-top:12px"><span>匹配说明（留痕）</span>
          <input v-model="l1Form.comment" placeholder="如：人工核对渠道对账单与订单号一致" />
        </label>
        <div class="modal-actions">
          <button @click="manualL1 = null">取消</button>
          <button class="primary" @click="submitManualL1">确认手工匹配</button>
        </div>
      </div>
    </div>

    <!-- LEVEL2 拆分弹窗 -->
    <div v-if="split" class="modal-mask" @click.self="split = false">
      <div class="modal" style="width:720px">
        <h3>手工 LEVEL2 拆分挂账</h3>
        <label class="field"><span>银行流水</span>
          <select v-model="l2Form.bank_transaction_id" @change="onBankChange">
            <option v-for="b in ws.level2.banks" :key="b.bank_transaction_id" :value="b.bank_transaction_id">
              {{ b.bank_ref }} · {{ b.legal_entity }} · 残差 {{ b.residual_amount }} {{ b.currency }}
            </option>
          </select>
        </label>
        <div v-if="selectedBank && selectedBank.cross_entity" class="warn-banner">
          该流水收款主体 {{ selectedBank.legal_entity }} 与结算主体 {{ ws.batch.legal_entity }} 不一致，
          跨主体差异系统不会自动核销，须人工核实并勾选下方确认。
        </div>
        <label class="field"><span>拆分方式</span>
          <select v-model="l2Form.split_rule">
            <option value="FULL">FULL 全额（单行）</option>
            <option value="RATIO">RATIO 按金额比例（尾差并入末行）</option>
            <option value="DETAIL">DETAIL 指定每条明细金额</option>
          </select>
        </label>

        <div class="table-wrap">
          <table class="data">
            <thead><tr><th></th><th>结算行</th><th>类型</th><th class="num">净额</th><th class="num">未匹配余额</th>
                    <th v-if="l2Form.split_rule === 'RATIO'" class="num">比例权重</th>
                    <th v-if="l2Form.split_rule === 'DETAIL'" class="num">指定金额</th></tr></thead>
            <tbody>
              <tr v-for="l in splittableLines" :key="l.line_id">
                <td><input type="checkbox" :value="l.line_id" v-model="l2Form.line_ids" style="width:auto"
                           :disabled="l2Form.split_rule === 'FULL'" /></td>
                <td class="mono">{{ l.line_ref }}</td>
                <td>{{ lineTypeLabel(l.line_type) }}</td>
                <td class="num">{{ fmt(l.net_amount) }}</td>
                <td class="num">{{ fmt(l.remaining_amount) }}</td>
                <td v-if="l2Form.split_rule === 'RATIO'" class="num">
                  <input type="number" step="0.01" v-model="l2Form.ratios[l.line_id]" style="width:110px" />
                </td>
                <td v-if="l2Form.split_rule === 'DETAIL'" class="num">
                  <input type="number" step="0.01" v-model="l2Form.amounts[l.line_id]" style="width:110px" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted" style="font-size:12px;margin:8px 0">
          本笔分配合计 <b>{{ fmt(splitTotal) }}</b>；流水可用残差
          <b>{{ selectedBank ? selectedBank.residual_amount : '0.00' }}</b>
          ；未覆盖余额将自动生成结转挂账。
        </p>

        <label class="field"><span>消化既有结转挂账（可选）</span>
          <select v-model="l2Form.carry_forward_id">
            <option :value="null">不关联</option>
            <option v-for="c in openCarries" :key="c.id" :value="c.id">
              #{{ c.id }} {{ reasonLabel(c.reason) }} · {{ c.amount }} {{ c.currency }}
            </option>
          </select>
        </label>
        <label class="field" v-if="selectedBank && selectedBank.cross_entity">
          <span style="display:flex;align-items:center;gap:8px">
            <input type="checkbox" v-model="l2Form.ack" style="width:auto" />
            我已人工核实该跨主体资金调拨，确认人工核销并留痕（系统不自动核销）
          </span>
        </label>
        <label class="field"><span>匹配说明（留痕）</span>
          <input v-model="l2Form.comment" placeholder="如：按订单金额比例拆分部分到账，差额待下月补付" />
        </label>
        <div class="modal-actions">
          <button @click="split = false">取消</button>
          <button class="primary" @click="submitSplit">提交拆分匹配</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()

const batches = ref([])
const batchId = ref(null)
const ws = ref(null)
const operator = ref('finance.chen')
const openDetail = reactive({})

const manualL1 = ref(null)
const candidateEvents = ref([])
const l1Form = reactive({ event_id: null, comment: '' })

const split = ref(false)
const l2Form = reactive({
  bank_transaction_id: null, split_rule: 'FULL', line_ids: [],
  ratios: {}, amounts: {}, ack: false, comment: '', carry_forward_id: null,
})

const fmt = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
const lineTypeLabel = (t) => ({
  CAPTURE: '捕获', REFUND: '退款', CHARGEBACK: '拒付',
  CHARGEBACK_REVERSAL: '拒付反转', FEE: '手续费',
  RESERVE: '保证金扣留', RESERVE_RELEASE: '保证金释放', ADJUSTMENT: '调整项',
}[t] || t)
const componentLabel = (k) => ({
  FEE: '手续费', RESERVE: '保证金', RESERVE_RELEASE: '保证金释放', ADJUSTMENT: '调整项',
}[k] || k)
const basisLabel = (b) => ({
  ORDER_REF_EXACT: '订单号精确', CHANNEL_REF_FALLBACK: '渠道号回退',
  AMOUNT_EXACT: '金额精确', MERGED_GREEDY: '合并贪心建议',
  MANUAL_SELECTION: '人工选择', RATIO_SPLIT: '比例拆分',
  DETAIL_SPLIT: '明细拆分', CROSS_ENTITY_MANUAL: '跨主体人工核销',
}[b] || b || '—')
const splitLabel = (s) => ({ FULL: '全额', RATIO: '比例拆分', DETAIL: '明细拆分', CARRY_FORWARD: '结转' }[s] || s)
const statusLabel = (s) => ({ PROPOSED: '建议待确认', CONFIRMED: '已确认', REJECTED: '已拒收' }[s] || s)
const reasonLabel = (r) => ({
  SHORT_FALL: '入账不足结转', RESIDUAL: '流水残差结转',
  PARTIAL_SPLIT: '拆分余额结转', CROSS_ENTITY: '跨主体挂账',
}[r] || r)
const confClass = (c) => {
  const v = Number(c || 0)
  return v >= 90 ? 'conf-high' : v >= 70 ? 'conf-mid' : 'conf-low'
}

const selectedBank = computed(() =>
  ws.value?.level2.banks.find((b) => b.bank_transaction_id === l2Form.bank_transaction_id) || null)
const splittableLines = computed(() =>
  (ws.value?.level2.lines || []).filter((l) => Number(l.net_amount) > 0 && Number(l.remaining_amount) !== 0))
const openCarries = computed(() => (ws.value?.carry_forwards || []).filter((c) => c.status === 'OPEN'))
const splitTotal = computed(() => {
  const avail = Number(selectedBank.value?.residual_amount || 0)
  if (l2Form.split_rule === 'DETAIL') {
    return l2Form.line_ids.reduce((s, id) => s + Number(l2Form.amounts[id] || 0), 0)
  }
  if (l2Form.split_rule === 'RATIO') {
    // 预计分配合计 = 所选行未匹配余额之和，受流水残差上限约束（后端按权重归一化，尾差并入末行）
    const lineSum = l2Form.line_ids.reduce(
      (s, id) => s + Number(splittableLines.value.find((l) => l.line_id === id)?.remaining_amount || 0), 0)
    return Math.min(lineSum, avail)
  }
  const line = splittableLines.value.find((l) => l.line_id === l2Form.line_ids[0])
  return line ? Math.min(Number(line.remaining_amount), avail) : 0
})

function toggleDetail(key) { openDetail[key] = !openDetail[key] }

function toast(msg, ok = true) {
  const el = document.createElement('div')
  el.className = `toast ${ok ? 'ok' : 'err'}`
  el.textContent = msg
  document.body.appendChild(el)
  setTimeout(() => el.remove(), 3200)
}

async function loadBatches() {
  batches.value = await api.batches()
  if (!batchId.value && batches.value.length) {
    batchId.value = batches.value.find((b) => b.status === 'OPEN')?.id || batches.value[0].id
  }
}
async function loadWorkspace() {
  if (!batchId.value) return
  ws.value = await api.matchingWorkspace(batchId.value)
}
async function runAuto() {
  try {
    const r = await api.reconcile(batchId.value)
    toast(`自动对账完成：未关闭差异 ${r.discrepancies_open} 条（仅建议，未核销）`)
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}

async function confirmL1(m) {
  const comment = prompt('确认该订单→结算行匹配？可填写匹配依据说明', '订单号与渠道参考号核对一致')
  if (comment === null) return
  try {
    await api.confirmMatching(m.id, { operator: operator.value, comment })
    toast('已人工确认，置信度 100 并留痕')
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}
async function confirmL2(m) {
  const cross = m.bank_legal_entity && m.bank_legal_entity !== ws.value.batch.legal_entity
  let ack = false
  if (cross) {
    ack = confirm('该匹配涉及跨主体差异，系统不会自动核销。确认已人工核实并手工核销？')
    if (!ack) return
  }
  const comment = prompt('确认该结算行→银行流水匹配？可填写匹配依据说明', cross ? '跨主体资金调拨已人工核实' : '银行回单与结算明细核对一致')
  if (comment === null) return
  try {
    await api.confirmMatching(m.id, {
      operator: operator.value, comment, acknowledge_cross_entity: ack,
    })
    toast('已人工确认并记录匹配依据')
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}
async function reject(m) {
  const comment = prompt('拒收该自动建议的原因（将留痕，建议额度立即释放）', '建议依据不充分')
  if (!comment) return
  try {
    await api.rejectMatching(m.id, operator.value, comment)
    toast('建议已拒收')
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}

async function openManualL1(row) {
  manualL1.value = row
  l1Form.event_id = null
  l1Form.comment = ''
  const all = await api.businessEvents({ limit: 500 })
  candidateEvents.value = all.filter((e) =>
    ['CAPTURE', 'REFUND', 'CHARGEBACK', 'AUTHORIZATION'].includes(e.entity_type))
}
async function submitManualL1() {
  if (!l1Form.event_id) return toast('请选择业务事件', false)
  try {
    await api.manualLevel1({
      operator: operator.value,
      business_event_id: l1Form.event_id,
      settlement_line_id: manualL1.value.line_id,
      comment: l1Form.comment,
    })
    manualL1.value = null
    toast('手工匹配已建立（置信度 100）')
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}

function onBankChange() {
  l2Form.line_ids = []
  l2Form.ack = false
}
function openSplit() {
  split.value = true
  Object.assign(l2Form, {
    bank_transaction_id: ws.value.level2.banks[0]?.bank_transaction_id || null,
    split_rule: 'FULL', line_ids: [], ratios: {}, amounts: {},
    ack: false, comment: '', carry_forward_id: null,
  })
}
async function submitSplit() {
  if (!l2Form.bank_transaction_id) return toast('请选择银行流水', false)
  if (!l2Form.line_ids.length) return toast('请选择至少一条结算行', false)
  if (l2Form.split_rule === 'FULL' && l2Form.line_ids.length !== 1) {
    return toast('FULL 方式只能选择一条结算行', false)
  }
  if (selectedBank.value.cross_entity && !l2Form.ack) {
    return toast('跨主体匹配必须先勾选人工核销确认', false)
  }
  const items = l2Form.line_ids.map((id) => {
    if (l2Form.split_rule === 'RATIO') return { settlement_line_id: id, ratio: l2Form.ratios[id] }
    if (l2Form.split_rule === 'DETAIL') return { settlement_line_id: id, amount: l2Form.amounts[id] }
    return { settlement_line_id: id }
  })
  if (l2Form.split_rule === 'RATIO' && items.some((i) => !Number(i.ratio))) {
    return toast('请为每行填写比例权重', false)
  }
  if (l2Form.split_rule === 'DETAIL' && items.some((i) => !Number(i.amount))) {
    return toast('请为每行填写指定金额', false)
  }
  try {
    const r = await api.manualLevel2({
      operator: operator.value,
      bank_transaction_id: l2Form.bank_transaction_id,
      split_rule: l2Form.split_rule,
      items, comment: l2Form.comment,
      acknowledge_cross_entity: l2Form.ack,
      carry_forward_id: l2Form.carry_forward_id,
    })
    split.value = false
    toast(`已建立 ${r.count} 条人工拆分匹配，未匹配余额已结转挂账`)
    await loadWorkspace()
  } catch (e) { toast(e.message, false) }
}

onMounted(async () => {
  await loadBatches()
  if (route.query.batch) batchId.value = Number(route.query.batch)
  await loadWorkspace()
})
</script>
