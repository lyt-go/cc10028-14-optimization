import { request } from './client'

/** 批次表格抽取凭据相关类型，字段与后端 TableExtractService 契约对齐。 */
export type LayoutValidation = 'valid' | 'layout_missing' | 'stale_layout'
export type SubmitStatus = 'pending' | 'submitted' | 'blocked'

export interface CredentialSummary {
  total: number
  valid: number
  layout_missing: number
  stale_layout: number
  submitted: number
  blocked: number
}

export interface DocConfirmationItem {
  doc_id: string
  validation: LayoutValidation
  submit_status: SubmitStatus
  layout_version: string
  submitted_version: string | null
  tables: number
  cells: number
  next_action?: string
  reason?: string
  failure_pages?: number[]
  newer_version_available?: string
}

export interface GroupConfirmation {
  credential_id: string
  batch_key: string
  layout_set_version: string
  summary: CredentialSummary
  group_ready: boolean
  confirmable_docs: DocConfirmationItem[]
  individually_processable_docs: DocConfirmationItem[]
  blocked_docs: DocConfirmationItem[]
}

export interface SubmitResult {
  ok: boolean
  batch_key: string
  doc_id: string
  result: string
  message?: string
  layout_version?: string
  tables?: number
  cells?: number
  output?: string
  receipt_id?: string
  retry_receipt_id?: string
}

export interface CorrectedRegion {
  doc_id: string
  page: number
  old_bbox: string
  corrected_bbox: string
  layout_version: string
}

export interface RerunResult {
  batch_key: string
  doc_id: string
  result: string
  previous_version: string
  layout_version: string
  pages_rerun: number[]
  corrected_regions: CorrectedRegion[]
  receipt_id: string
}

export interface Receipt {
  receipt_id: string
  batch_key?: string
  doc_id: string
  action: string
  result: string
  layout_version?: string
  layout_checksum?: string
  tables?: number
  cells?: number
  reason?: string
  at?: number
  [field: string]: unknown
}

export interface RegionTraceItem {
  doc_id: string
  page: number
  layout_version: string
  region_status: string
  table_bboxes: string[]
  image_bboxes: string[]
  layout_checksum: string
  rerun_reason?: string
}

export interface CellVersion {
  layout_version: string
  file: string
  cell_count: number
  tables: string[]
  sample_cells: Record<string, unknown>[]
}

export interface TableTrace {
  doc_id: string
  current_layout_version: string
  region_trace: RegionTraceItem[]
  cell_versions: CellVersion[]
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* 忽略非 JSON 错误体 */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

function base(batchKey: string): string {
  return `/api/batch-tables/credentials/${encodeURIComponent(batchKey)}`
}

export async function createCredential(
  batchKey: string,
  docIds?: string[],
): Promise<GroupConfirmation> {
  const response = await request('/api/batch-tables/credentials', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey, doc_ids: docIds ?? null }),
  })
  return asJson<GroupConfirmation>(response)
}

export async function validateLayouts(batchKey: string): Promise<GroupConfirmation> {
  const response = await request(`${base(batchKey)}/validate`, { method: 'POST' })
  return asJson<GroupConfirmation>(response)
}

export async function getConfirmation(batchKey: string): Promise<GroupConfirmation> {
  const response = await request(`${base(batchKey)}/confirmation`)
  return asJson<GroupConfirmation>(response)
}

export async function submitDocTables(
  batchKey: string,
  docId: string,
  layoutVersion?: string,
): Promise<SubmitResult> {
  const response = await request(`${base(batchKey)}/docs/${encodeURIComponent(docId)}/submit`, {
    method: 'POST',
    body: JSON.stringify({ layout_version: layoutVersion ?? null }),
  })
  return asJson<SubmitResult>(response)
}

export async function retryDoc(batchKey: string, docId: string): Promise<SubmitResult> {
  const response = await request(`${base(batchKey)}/docs/${encodeURIComponent(docId)}/retry`, {
    method: 'POST',
  })
  return asJson<SubmitResult>(response)
}

export async function rerunRegions(
  batchKey: string,
  docId: string,
  failedPages: number[],
): Promise<RerunResult> {
  const response = await request(
    `${base(batchKey)}/docs/${encodeURIComponent(docId)}/rerun-regions`,
    { method: 'POST', body: JSON.stringify({ failed_pages: failedPages }) },
  )
  return asJson<RerunResult>(response)
}

export async function listReceipts(
  batchKey: string,
  docId?: string,
): Promise<{ batch_key: string; total: number; receipts: Receipt[] }> {
  const query = docId ? `?doc_id=${encodeURIComponent(docId)}` : ''
  const response = await request(`${base(batchKey)}/receipts${query}`)
  return asJson(response)
}

export async function traceTable(docId: string, tableKey?: string): Promise<TableTrace> {
  const query = tableKey ? `?table_key=${encodeURIComponent(tableKey)}` : ''
  const response = await request(
    `/api/batch-tables/docs/${encodeURIComponent(docId)}/trace${query}`,
  )
  return asJson<TableTrace>(response)
}
