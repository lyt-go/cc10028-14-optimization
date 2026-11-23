<template>
  <section class="page batch-page">
    <header class="page-head">
      <div>
        <h2>批次表格抽取凭据</h2>
        <p class="page-desc">
          一组版式数据先校验再逐文档提交：缺版式阻断、旧版式拒绝、好文档幂等不翻倍，
          失败区域可关联重跑并按补齐版本追回单元格。
        </p>
      </div>
    </header>

    <form class="credential-form" @submit.prevent="onCreate">
      <label class="form-field">
        <span>批次编号</span>
        <input v-model="batchKey" data-testid="cred-batch" placeholder="如 FILE-0001" required />
      </label>
      <label class="form-field grow">
        <span>文档清单（可选，逗号分隔；留空按批次登记解析）</span>
        <input v-model="docListText" data-testid="cred-docs" placeholder="doc-a.pdf, doc-b.pdf" />
      </label>
      <button class="btn primary" data-testid="cred-create" type="submit" :disabled="loading">
        {{ loading ? '处理中…' : '建立/打开凭据' }}
      </button>
      <button class="btn" type="button" :disabled="!confirmation" @click="onValidate">
        重新校验版式
      </button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="cred-error">{{ errorMessage }}</p>

    <template v-if="confirmation">
      <div class="stat-row" data-testid="cred-summary">
        <article class="stat-card">
          <span class="stat-label">整组文档</span>
          <strong class="stat-value">{{ confirmation.summary.total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">版式有效</span>
          <strong class="stat-value">{{ confirmation.summary.valid }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺版式</span>
          <strong class="stat-value warn">{{ confirmation.summary.layout_missing }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">旧版式</span>
          <strong class="stat-value warn">{{ confirmation.summary.stale_layout }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已提交</span>
          <strong class="stat-value ok">{{ confirmation.summary.submitted }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">阻断中</span>
          <strong class="stat-value warn">{{ confirmation.summary.blocked }}</strong>
        </article>
      </div>

      <p class="group-banner" :class="confirmation.group_ready ? 'ok' : 'warn'">
        整组目标版式版本：{{ confirmation.layout_set_version || '—' }} ｜
        <template v-if="confirmation.group_ready">全部文档已按当前版本确认，可整组交付</template>
        <template v-else>整组尚未就绪：好文档可单独确认，阻断文档需补齐版式后处理</template>
      </p>

      <div class="doc-columns">
        <section class="doc-col">
          <h3>可加入整组确认（{{ confirmation.confirmable_docs.length }}）</h3>
          <p class="col-hint">已确认成果仍可核验，重试不会重复写出。</p>
          <article v-for="doc in confirmation.confirmable_docs" :key="doc.doc_id" class="doc-card ok">
            <header>
              <strong>{{ doc.doc_id }}</strong>
              <span class="tag tag-ok">已提交 {{ doc.submitted_version }}</span>
            </header>
            <p class="doc-meta">{{ doc.tables }} 张表 ｜ {{ doc.cells }} 个单元格 ｜ 校验 {{ labelOf(doc.validation) }}</p>
            <p v-if="doc.newer_version_available" class="upgrade-hint">
              整组已有 {{ doc.newer_version_available }} 版式；当前成果仍可核验，如需对齐可对该文档重跑后补抽。
            </p>
            <div class="doc-actions">
              <button class="link" type="button" @click="onRetry(doc.doc_id)">重试（幂等）</button>
              <button class="link" type="button" @click="onTrace(doc.doc_id)">回查版本/区域</button>
              <button class="link" type="button" @click="onShowReceipts(doc.doc_id)">查看回执</button>
            </div>
          </article>
        </section>

        <section class="doc-col">
          <h3>可单独处理（{{ confirmation.individually_processable_docs.length }}）</h3>
          <p class="col-hint">版式齐全、尚未提交，可逐文档先确认。</p>
          <article v-for="doc in confirmation.individually_processable_docs" :key="doc.doc_id" class="doc-card">
            <header>
              <strong>{{ doc.doc_id }}</strong>
              <span class="tag">待提交 {{ doc.layout_version }}</span>
            </header>
            <p class="doc-meta">校验 {{ labelOf(doc.validation) }}</p>
            <div class="doc-actions">
              <button class="link" type="button" @click="onSubmit(doc.doc_id)">提交表格</button>
              <button class="link" type="button" @click="onTrace(doc.doc_id)">回查版本/区域</button>
            </div>
          </article>
        </section>

        <section class="doc-col">
          <h3>阻断文档（{{ confirmation.blocked_docs.length }}）</h3>
          <p class="col-hint">缺版式需先还原；旧版式需重跑补齐，旧区域不会被提交为新结果。</p>
          <article v-for="doc in confirmation.blocked_docs" :key="doc.doc_id" class="doc-card blocked">
            <header>
              <strong>{{ doc.doc_id }}</strong>
              <span class="tag tag-warn">{{ labelOf(doc.validation) }}</span>
            </header>
            <p class="doc-meta">
              当前版本 {{ doc.layout_version || '无' }} ｜
              失败页 {{ doc.failure_pages?.length ? doc.failure_pages.join('、') : '未登记' }}
            </p>
            <div class="rerun-row">
              <input
                v-model="rerunPages[doc.doc_id]"
                class="page-input"
                placeholder="失败页，如 1,3"
                aria-label="失败页码"
              />
              <button class="link" type="button" @click="onRerun(doc.doc_id)">关联区域重跑</button>
            </div>
            <div class="doc-actions">
              <button class="link" type="button" @click="onSubmit(doc.doc_id)">尝试提交</button>
              <button class="link" type="button" @click="onShowReceipts(doc.doc_id)">查看回执</button>
            </div>
          </article>
        </section>
      </div>

      <section class="panel">
        <header class="panel-head">
          <h3>逐条回执 <span v-if="receiptFilter" class="col-hint">（已按文档过滤：{{ receiptFilter }}）</span></h3>
          <button v-if="receiptFilter" class="btn ghost" type="button" @click="receiptFilter = ''">清除过滤</button>
        </header>
        <button class="btn" type="button" @click="onShowReceipts()">刷新全部回执</button>
        <table v-if="receipts.length" class="data-table receipt-table">
          <thead>
            <tr><th>回执编号</th><th>文档</th><th>动作</th><th>结果</th><th>版式版本</th><th>表/格</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="receipt in receipts" :key="receipt.receipt_id">
              <td class="mono">{{ receipt.receipt_id }}</td>
              <td>{{ receipt.doc_id }}</td>
              <td>{{ receipt.action }}</td>
              <td><span class="tag" :class="receiptTagClass(receipt.result)">{{ receipt.result }}</span></td>
              <td>{{ receipt.layout_version || '—' }}</td>
              <td>{{ receipt.tables ?? 0 }} / {{ receipt.cells ?? 0 }}</td>
              <td class="reason">{{ receipt.reason || '' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="col-hint">暂无回执，提交或重跑后在此逐条可见。</p>
      </section>

      <section v-if="trace" class="panel">
        <header class="panel-head">
          <h3>表格血缘回查：{{ trace.doc_id }}（当前版式 {{ trace.current_layout_version }}）</h3>
          <button class="btn ghost" type="button" @click="trace = null">关闭</button>
        </header>
        <table class="data-table">
          <thead>
            <tr><th>页码</th><th>版式版本</th><th>区域状态</th><th>表格区域坐标</th><th>版式校验值</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in trace.region_trace" :key="`${item.page}-${item.layout_version}`">
              <td>{{ item.page }}</td>
              <td>{{ item.layout_version }}</td>
              <td><span class="tag" :class="item.region_status === 'corrected' ? 'tag-warn' : ''">{{ item.region_status }}</span></td>
              <td class="mono">{{ item.table_bboxes.join('；') || '—' }}</td>
              <td class="mono">{{ item.layout_checksum }}</td>
            </tr>
          </tbody>
        </table>
        <h4 class="cell-title">已写入单元格明细（按版本）</h4>
        <table class="data-table">
          <thead>
            <tr><th>版式版本</th><th>单元格数</th><th>表格键</th><th>产物文件</th></tr>
          </thead>
          <tbody>
            <tr v-for="version in trace.cell_versions" :key="version.layout_version">
              <td>{{ version.layout_version }}</td>
              <td>{{ version.cell_count }}</td>
              <td class="mono">{{ version.tables.join('、') }}</td>
              <td class="mono">{{ version.file }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  createCredential,
  getConfirmation,
  listReceipts,
  rerunRegions,
  retryDoc,
  submitDocTables,
  traceTable,
  validateLayouts,
} from '@/api/batch-tables'
import type {
  GroupConfirmation,
  LayoutValidation,
  Receipt,
  TableTrace,
} from '@/api/batch-tables'

const batchKey = ref('')
const docListText = ref('')
const loading = ref(false)
const errorMessage = ref('')
const confirmation = ref<GroupConfirmation | null>(null)
const receipts = ref<Receipt[]>([])
const receiptFilter = ref('')
const trace = ref<TableTrace | null>(null)
const rerunPages = ref<Record<string, string>>({})

const VALIDATION_LABELS: Record<LayoutValidation, string> = {
  valid: '版式有效',
  layout_missing: '缺版式',
  stale_layout: '旧版式',
}

function labelOf(value: string): string {
  return VALIDATION_LABELS[value as LayoutValidation] ?? value
}

function receiptTagClass(result: string): string {
  if (result === 'submitted' || result === 'layout_rerun') return 'tag-ok'
  if (result.startsWith('blocked') || result.startsWith('rejected')) return 'tag-warn'
  return ''
}

function parseDocIds(): string[] | undefined {
  const ids = docListText.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  return ids.length ? ids : undefined
}

async function runWithGuard<T>(action: () => Promise<T>): Promise<T | null> {
  loading.value = true
  errorMessage.value = ''
  try {
    return await action()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次凭据操作失败'
    return null
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  if (!batchKey.value.trim()) return
  const result = await runWithGuard(() =>
    createCredential(batchKey.value.trim(), parseDocIds()),
  )
  // 凭据已存在时回退为打开现有凭据
  if (!result && /已存在/.test(errorMessage.value)) {
    const existing = await runWithGuard(() => getConfirmation(batchKey.value.trim()))
    if (existing) confirmation.value = existing
    return
  }
  if (result) confirmation.value = result
}

async function onValidate() {
  if (!confirmation.value) return
  const result = await runWithGuard(() => validateLayouts(confirmation.value!.batch_key))
  if (result) confirmation.value = result
}

async function refreshReceipts(docId = '') {
  if (!confirmation.value) return
  receiptFilter.value = docId
  const result = await runWithGuard(() =>
    listReceipts(confirmation.value!.batch_key, docId || undefined),
  )
  if (result) receipts.value = result.receipts
}

async function refreshConfirmation() {
  if (!confirmation.value) return
  confirmation.value = await getConfirmation(confirmation.value.batch_key)
}

async function onSubmit(docId: string) {
  if (!confirmation.value) return
  const result = await runWithGuard(() =>
    submitDocTables(confirmation.value!.batch_key, docId),
  )
  if (result) {
    await refreshConfirmation()
    await refreshReceipts(receiptFilter.value)
  }
}

async function onRetry(docId: string) {
  if (!confirmation.value) return
  const result = await runWithGuard(() => retryDoc(confirmation.value!.batch_key, docId))
  if (result) {
    await refreshConfirmation()
    await refreshReceipts(receiptFilter.value)
  }
}

async function onRerun(docId: string) {
  if (!confirmation.value) return
  const pages = (rerunPages.value[docId] ?? '')
    .split(',')
    .map((item) => Number(item.trim()))
    .filter((num) => Number.isInteger(num) && num > 0)
  if (!pages.length) {
    errorMessage.value = '请先填写至少一个失败页码'
    return
  }
  const result = await runWithGuard(() =>
    rerunRegions(confirmation.value!.batch_key, docId, pages),
  )
  if (result) {
    await refreshConfirmation()
    await refreshReceipts(receiptFilter.value)
  }
}

async function onShowReceipts(docId = '') {
  await refreshReceipts(docId)
}

async function onTrace(docId: string) {
  const result = await runWithGuard(() => traceTable(docId))
  if (result) trace.value = result
}
</script>

<style scoped>
.credential-form {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 12px;
}
.form-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-field.grow { flex: 1; min-width: 240px; }
.form-field input { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.group-banner { padding: 10px 12px; border-radius: 8px; font-size: 13px; margin: 4px 0 14px; }
.group-banner.ok { background: #ecfdf3; color: #027a48; border: 1px solid #abefc6; }
.group-banner.warn { background: #fffaeb; color: #b54708; border: 1px solid #fedf89; }
.doc-columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.doc-col h3 { font-size: 14px; margin: 0 0 4px; }
.col-hint { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
.doc-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #fff; margin-bottom: 10px; }
.doc-card.ok { border-left: 3px solid #22c55e; }
.doc-card.blocked { border-left: 3px solid #ef4444; }
.doc-card header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.doc-meta { font-size: 12px; color: var(--muted); margin: 6px 0; word-break: break-all; }
.upgrade-hint { font-size: 12px; color: #b54708; background: #fffaeb; border: 1px solid #fedf89; border-radius: 6px; padding: 6px 8px; margin: 6px 0; }
.doc-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.rerun-row { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.page-input { width: 110px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #f1f5f9; font-size: 11px; }
.tag-ok { background: #dcfae6; color: #027a48; }
.tag-warn { background: #fee4e2; color: #b42318; }
.stat-value.warn { color: #b54708; }
.stat-value.ok { color: #027a48; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-top: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.receipt-table .reason { max-width: 320px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.cell-title { font-size: 13px; margin: 14px 0 6px; }
@media (max-width: 1100px) { .doc-columns { grid-template-columns: 1fr; } }
</style>
