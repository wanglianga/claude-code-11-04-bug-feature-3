# 订单到清结算差异处理中心

跨境电商财务的订单 → 支付 → 退款/拒付 → 渠道结算 → 银行入账全链路差异处置系统。
FastAPI + PostgreSQL（四层数据架构）+ Vue 3，解决重复记账、乱序状态破坏、手续费/汇率后补、
合并拆分入账、关闭周期被静默改写等问题。

## 一、架构与关键约束

| 层 | 表 | 职责 |
|---|---|---|
| L1 原始事件层 | `raw_events` | CDC/webhook/轮询/人工消息原样留存，`(source, idempotency_key)` 去重 |
| L2 标准业务账本层 | `business_events` `ledger_entries` `fx_rates` `order_states` `payment_states` | 归一化业务事件、双分录三币种账本、历史汇率版本、按业务时间重放的状态投影 |
| L3 结算匹配层 | `settlement_batches/lines` `bank_transactions` `settlement_matches` `match_carry_forwards` `discrepancies(+actions)` | 两层匹配、五类差异、拆分/合并/结转、跨主体拦截、人工处置工作流 |
| L4 月度汇总层 | `monthly_reports` `monthly_report_versions` | 不可变快照与版本链，更正只追加新版本 |

关键机制：
- **两层幂等**：L1 来源消息去重 + L2 `business_key` 去重，CDC 重放/重复 webhook/轮询补拉均不重复进账本。
- **乱序安全**：状态投影按业务发生时间 `event_time` 重放，迟到的旧事件不能回退订单/支付状态。
- **三币种留痕**：每笔金额保留交易币、结算币、记账币，以及汇率来源、适用时点、精度规则和原始金额。
- **只追加账本**：6 张证据表由 PostgreSQL 触发器拒绝 UPDATE/DELETE/TRUNCATE（详见 `db/DATA_LAYER.md`）。
- **自动任务只提示**：对账引擎只生成 `PROPOSED` 匹配与 `OPEN` 差异；`UNKNOWN` 未知差必须人工调账留痕后才能关闭。
- **两层人工匹配工作台**（前端“两层匹配工作台”页，API 前缀 `/api/matching`）：
  - 先确认 **LEVEL1 订单→结算行**，再确认 **LEVEL2 结算行→银行流水**；页面区分捕获/退款/拒付、
    手续费、保证金（RESERVE/RESERVE_RELEASE）与调整项（ADJUSTMENT），成分行不参与订单匹配；
  - 每条自动建议带**匹配依据与置信度**（订单号精确 95、渠道号回退 75、金额精确 90、合并贪心 60），
    人工确认/建立固定置信度 100 并记录确认人与说明；
  - **总额相等不等于逐笔完成**：完成态按每条结算行/流水的有效 CONFIRMED 分配与未结转余额逐笔判定，
    批次净额与银行入账合计相等但存在建议/跨主体/挂账时页面显式警示“总额相等陷阱”；
  - 部分匹配支持 **RATIO 按金额比例**（尾差并入末行）与 **DETAIL 指定明细** 拆分挂账，
    未匹配余额写入 `match_carry_forwards` 单独结转（SHORT_FALL/RESIDUAL/PARTIAL_SPLIT/CROSS_ENTITY）；
  - **跨主体不自动核销**：`legal_entity` 不一致的银行流水引擎不生成任何匹配，只提示 UNKNOWN 并挂账，
    人工核销必须显式勾选确认，留痕 `CROSS_ENTITY_MANUAL`；建议可拒收（REJECTED 留痕、额度释放）。
- **关闭后更正**：结算修订、汇率更正、补单一律追加更正账与新报表版本，旧快照标记 `SUPERSEDED` 但原样保留。

## 二、一键启动（Docker Compose，推荐）

前置：Docker + Docker Compose。

```bash
cd /workspace/04
docker compose up -d --build          # 启动 db / backend / frontend
docker compose run --rm seed          # 装载 9 类模拟场景并跑完整处置/更正/版本链
```

访问：
- 前端（财务处置队列、批次对账、事件账本、报表版本）：http://localhost:8080
- 后端 API：http://localhost:8000 ，健康检查 http://localhost:8000/health

停止与清库：
```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除数据库卷（彻底重置）
```

## 三、本地开发启动（无 Docker 时）

需要 Python 3.11+、Node 20+、PostgreSQL 14+。

### 3.1 准备数据库
```bash
createdb recon   # 或在已有实例中建库，账号密码按需调整
```

### 3.2 后端
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export RECON_DATABASE_URL="postgresql+asyncpg://<user>:<pwd>@<host>:5432/recon"
export PYTHONPATH="$(pwd)"
uvicorn app.main:app --reload --port 8000     # 启动时自动建表并安装只追加触发器
python scripts/seed.py --reset                 # 可选：装载模拟场景数据
```

### 3.3 前端
```bash
cd frontend
npm install
VITE_API_TARGET=http://localhost:8000 npm run dev    # http://localhost:5173，/api 自动代理
```

前端生产构建：`npm run build`（产物在 `frontend/dist`，容器内由 Nginx 托管并反代后端）。

## 四、测试账号 / 操作身份

系统为内部财务工具，处置动作以**操作人工号**标识（无独立登录体系）：
- 演示与种子脚本统一使用操作人：`finance.chen`
- 前端处置弹窗的“处理人”字段默认填充 `finance.chen`，可改为实际工号；所有认领、调账、关闭动作都带操作人留痕。
- 数据库连接（Compose）：用户 `recon` / 密码 `recon` / 库 `recon`。

## 五、验证方式

### 5.1 自动化验收（推荐）
```bash
cd backend && source .venv/bin/activate
export RECON_DATABASE_URL="postgresql+asyncpg://recon:recon@<host>:5432/recon"
bash scripts/verify.sh        # = pytest 全套 + seed 端到端
# 或分步：
python -m pytest tests/ -q    # 41 个用例
python scripts/seed.py --reset
```

测试覆盖（`backend/tests/`）：
| 文件 | 验收点 |
|---|---|
| `test_01_idempotency.py` | CDC 重放、重复 webhook、轮询补拉不重复进账本 |
| `test_02_ordering.py` | webhook 乱序下支付状态正确、旧订单状态迟到不回退 |
| `test_03_lifecycle.py` | 部分退款、拒付反转、账本借贷守恒、三币种与汇率溯源 |
| `test_04_reconciliation.py` | 两层匹配、五类差异归因、对账幂等、未知差禁止自动关闭、合并入账分配 |
| `test_05_workflow.py` | 认领/附件/调账建议/过账/关闭轨迹、关闭前置校验、快照生成 |
| `test_06_corrections.py` | 结算修订新版本、汇率更正账、报表版本链、历史快照不被改写、关闭后补单 |
| `test_07_append_only.py` | UPDATE/DELETE/TRUNCATE 触发器、证据列不可改、生命周期列可改 |
| `test_08_matching_workspace.py` | 总额相等陷阱、置信度/依据留痕、跨主体拦截与人工 ack、比例/明细拆分、结转挂账、拒收释放、成分行保护 |

### 5.2 模拟场景（`backend/app/demo/events.py`，结算周期 2026-10）
S1 CDC 重放 · S2 webhook 乱序与状态回退防护 · S3 支付后延迟入账（11-15 才到银行）·
S4 部分退款（ORD-005 退 40/100）· S5 拒付反转（ORD-006）· S6 结算单修订 v1→v2（手续费后补）·
S7 汇率更正 USD/CNY 7.20→7.25 · S8 多笔合并银行入账；另含金额差（ORD-007）、汇率差（ORD-009 EUR）、
幽灵结算行与银行残差两类未知差、关闭周期补单。
S9 第二主体 ADYEN/M2 批次 `SB-202610-M2`：保证金扣留行（RESERVE -15）与调整项行（ADJUSTMENT -5）、
部分到账（BK-M2-1 仅 200/280）、跨主体流水（BK-M2-2 收款主体 ENTITY-M1）；
两笔流水合计 280 恰好等于批次净额，但逐笔匹配**不得**标记完成——“总额相等陷阱”的固定演示数据。

### 5.3 本环境实际执行过的验证（结论如实记录）
- `python -m pytest tests/ -q`：**41 passed**（用户态 PostgreSQL 15，端口 5433；原 38 用例 + 本缺陷新增 3 个结转挂账回归用例）。
- `python scripts/seed.py --reset`：端到端跑通，31 条原始消息中 2 条重复被丢弃、生成 28 个业务事件；
  v1 批次产生 5 条差异（AMOUNT/FX/TIMING 各 1、UNKNOWN 2）并全部人工处置关闭；
  S9 的 M2 批次对账产生 2 条 UNKNOWN（跨主体 + 入账不足）并保持 OPEN 供工作台演示；
  最终生成 4 个报表版本（v1~v3 SUPERSEDED、v4 CURRENT），
  全账本借贷各 22,248.32 CNY 平衡；只追加触发器成功拦截 UPDATE/DELETE。
- 两层匹配工作台 API 冒烟（真实 HTTP 调用）：
  工作台返回 `bank_inflow_equals_net=true` 且 `complete=false` 的总额相等陷阱；
  3 条 LEVEL1 建议人工确认后置信度 100、依据与确认人留痕；
  跨主体手工匹配未勾选 ack 返回 400 拦截，勾选后落 `CROSS_ENTITY_MANUAL`；
  拒收 PROPOSED 建议后 RATIO 比例拆分 200 → 109.09/90.91（尾差并入末行），
  两行余额 7.91/6.59 自动结转挂账；重跑对账后引擎 SHORT_FALL 挂账自动 RESOLVED、人工 PARTIAL_SPLIT 挂账保留。
- 跨主体流水部分核销后结转余额重复缺陷的修复复测（真实 HTTP 调用，场景 S9 / 批次 `SB-202610-M2`）：
  修复前对 `BK-M2-2`（80，跨主体）DETAIL 只核销 30，原 `CROSS_ENTITY` 挂账仍挂 80 且新增一笔 50 的
  `PARTIAL_SPLIT` 挂账，同一余额被列两次（80+50）；修复后原挂账原地扣减为 **50.00**、不再新增，
  `GET /api/matching/workspace/1` 刷新与 `POST /api/batches/1/reconcile` 重跑后该流水都只剩**一条 50.00 的 OPEN 挂账，
  且金额等于流水实际残差**；继续核销剩余 50 后挂账 RESOLVED、未匹配列表不再出现该流水。
  同主体流水多次部分核销时挂账只原地更新（150→100）不重复登记；新增 3 个回归用例覆盖上述路径。
- 前端 `npm run build` 构建成功（91 modules）；Vite dev server + `/api` 代理联调返回真实数据。
- 说明：本环境无 Docker 守护进程，**未实际执行镜像构建**，Dockerfile/compose 已按标准编写并通过 YAML 校验，
  请在具备 Docker 的环境按第二节验证。

## 六、主要 API（FastAPI，OpenAPI 见 `/docs`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/events/ingest` `/api/events/ingest-batch` | 原始事件摄入（两层幂等） |
| POST | `/api/fx/rates` | 汇率发布/更正（自动版本号） |
| GET | `/api/events/raw` `/api/events/business` | L1/L2 事件查询 |
| GET | `/api/ledger/entries` `/api/ledger/balances` | 分录与守恒视图 |
| GET | `/api/states/orders` `/api/states/payments` | 状态投影 |
| POST | `/api/reconcile/run-all` | 自动对账（只生成建议） |
| GET/POST | `/api/batches`、`/api/batches/{id}/reconcile|close|snapshot` | 批次对账与关闭 |
| GET/POST | `/api/matches`、`/api/matches/{id}/confirm` | 两层匹配与人工确认 |
| GET | `/api/matching/workspace/{batch_id}` | 两层匹配工作台聚合视图（逐笔状态/成分行/结转/总额相等判定） |
| POST | `/api/matching/matches/{id}/confirm|reject` | 人工确认（跨主体须 ack）/ 拒收自动建议 |
| POST | `/api/matching/level1/manual` | 手工订单事件→结算行（MANUAL_SELECTION，置信度 100） |
| POST | `/api/matching/level2/manual` | 手工结算行→银行流水（FULL/RATIO/DETAIL 拆分、结转、跨主体 ack） |
| GET | `/api/matching/carry-forwards` | 未匹配余额结转挂账 |
| GET/POST | `/api/discrepancies`、`/{id}/claim|attach|adjust-suggest|adjust-post|close` | 差异处置队列 |
| GET | `/api/reports`、`/api/reports/{id}/versions` | 月度报表与版本快照 |
| POST | `/api/reports/regenerate` | 关闭周期补单后重算新版本 |

## 七、目录结构

```
.
├── backend/
│   ├── app/
│   │   ├── models.py            # 四层表结构与只追加清单
│   │   ├── enums.py             # 事件/状态/差异/科目枚举
│   │   ├── init_db.py           # 建表 + 只追加触发器
│   │   ├── services/
│   │   │   ├── ledger.py        # 幂等摄入、双分录、状态投影
│   │   │   ├── fx.py            # 历史汇率时点取值
│   │   │   ├── reconciliation.py# 两层匹配与五类差异引擎（建议/跨主体拦截/结转）
│   │   │   ├── matching.py      # 人工匹配工作台：确认/拒收/拆分挂账/结转留痕
│   │   │   ├── workflow.py      # 人工处置与批次关闭
│   │   │   └── corrections.py   # 更正账与报表版本
│   │   ├── routers/             # events / reconciliation / matching API
│   │   └── demo/                # 9 类模拟事件与场景装载器
│   ├── tests/                   # 41 个 pytest 验收用例
│   └── scripts/seed.py verify.sh
├── frontend/                    # Vue 3 + Vite + vue-router + axios（含两层匹配工作台页）
├── db/DATA_LAYER.md             # 数据层与触发器设计说明
└── docker-compose.yml           # db + backend + frontend(+seed profile)
```
