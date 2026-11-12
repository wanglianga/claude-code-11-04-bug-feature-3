import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 20000 })

api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const detail = err.response?.data?.detail || err.message
    return Promise.reject(new Error(detail))
  },
)

export default {
  // 摄入与查询
  ingest: (data) => api.post('/events/ingest', data),
  ingestBatch: (events) => api.post('/events/ingest-batch', { events }),
  rawEvents: (limit = 100) => api.get('/events/raw', { params: { limit } }),
  businessEvents: (params) => api.get('/events/business', { params }),
  ledgerEntries: (params) => api.get('/ledger/entries', { params }),
  ledgerBalances: () => api.get('/ledger/balances'),
  paymentStates: () => api.get('/states/payments'),
  orderStates: () => api.get('/states/orders'),
  runAllReconcile: () => api.post('/reconcile/run-all'),
  regenerateReports: (channel) => api.post('/reports/regenerate', null, { params: { channel } }),

  // 批次 / 匹配 / 银行
  batches: (status) => api.get('/batches', { params: { status } }),
  batch: (id) => api.get(`/batches/${id}`),
  reconcile: (id) => api.post(`/batches/${id}/reconcile`),
  closeBatch: (id, operator) => api.post(`/batches/${id}/close`, { operator }),
  snapshot: (id) => api.get(`/batches/${id}/snapshot`),
  matches: (params) => api.get('/matches', { params }),
  confirmMatch: (id, operator) => api.post(`/matches/${id}/confirm`, { operator }),
  bankTxns: () => api.get('/bank-transactions'),

  // 两层匹配工作台
  matchingWorkspace: (batchId) => api.get(`/matching/workspace/${batchId}`),
  confirmMatching: (id, payload) => api.post(`/matching/matches/${id}/confirm`, payload),
  rejectMatching: (id, operator, comment) =>
    api.post(`/matching/matches/${id}/reject`, { operator, comment }),
  manualLevel1: (payload) => api.post('/matching/level1/manual', payload),
  manualLevel2: (payload) => api.post('/matching/level2/manual', payload),
  carryForwards: (params) => api.get('/matching/carry-forwards', { params }),

  // 差异处置
  discrepancies: (params) => api.get('/discrepancies', { params }),
  discrepancy: (id) => api.get(`/discrepancies/${id}`),
  claim: (id, operator, comment) => api.post(`/discrepancies/${id}/claim`, { operator, comment }),
  attach: (id, operator, attachment, comment) =>
    api.post(`/discrepancies/${id}/attach`, { operator, attachment, comment }),
  suggest: (id, operator, adjust_amount, account, comment) =>
    api.post(`/discrepancies/${id}/adjust-suggest`, {
      operator, adjust_amount, account, comment,
    }),
  postAdjust: (id, operator, comment) =>
    api.post(`/discrepancies/${id}/adjust-post`, { operator, comment }),
  closeDisc: (id, operator, comment) =>
    api.post(`/discrepancies/${id}/close`, { operator, comment }),

  // 报表
  reports: () => api.get('/reports'),
  reportVersions: (id) => api.get(`/reports/${id}/versions`),
}
