# 离线网络安全控制面模型 —— 多区域代理灰度发布的租户隔离检查器

一个**仅使用 Python 3.12 标准库、纯 CPU、确定性**的离线模型。它在合成的多区域服务代理
拓扑上传播**无载荷的抽象身份标记**，用于回答两类问题：

1. **`replay`（复演）**：给定一份完全定时的事件序列（控制动作、可选的单故障域窗口、
   具体链路延迟），逐离散步仿真标记的生命周期，输出每个代次屏障的排空见证、所有投递/
   丢弃/异常回执，以及**最早违规前缀**。
2. **`plan`（规划）**：在所有输入允许的链路延迟组合与单故障域情形下，搜索一张**固定的
   灰度发布表**，使其对每一情形都同时满足租户隔离安全与请求必达性，并按
   “停用代理总时长 → 发布步数 → 动作 ID 序列”最小化。

> 全部拓扑、身份、租户与标记均为虚构合成数据；标记只携带身份标签，没有任何业务载荷。

---

## 1. 运行环境与快速开始

- Windows 原生 Python 3.12（已在 3.12.8 验证），无第三方依赖。
- 从仓库根目录运行（`solution` 包位于根目录）：

```bat
python -m solution replay examples\01_safe_gray.json
python -m solution plan   examples\02_leak_window.json
python -m solution validate examples\01_safe_gray.json
```

stdout 始终输出**确定性的 UTF-8 JSON**（键排序、固定缩进）。结构异常不抛栈，而是返回
带字段路径（`path`，`$` 表示文档根）与稳定错误码（`code`）的信封：

```json
{ "status": "error", "error": { "code": "ILLEGAL_VALUE",
  "message": "ttl must be an integer in [1, 6]", "path": "$.requests[0].ttl" } }
```

运行测试：

```bat
python -m unittest discover -s tests -v
```

重新生成六个样例 JSON（可选，仓库已附带生成结果）：

```bat
cd examples
python generate_examples.py
```

---

## 2. 目录结构

```
solution/
  errors.py    错误码与结构化校验异常
  model.py     类型化用例模型 + 严格输入校验（字段路径错误码）
  engine.py    确定性离散事件标记仿真器（TTL、代次、屏障排空、暂停、故障域）
  replay.py    replay 命令
  planner.py   plan 命令（情形包络、分量分解搜索、静态预检、穷举对拍）
  cli.py       命令行入口与 JSON 信封
examples/
  _builder.py, generate_examples.py   六个合成样例的构造器
  01..06_*.json                        可直接运行的样例输入
  scale_build.py                       14 代理 / 16 动作上限规模夹具
tests/         unittest 套件（49 个断言用例）
```

硬性上限（在 `solution/model.py` 中强制）：代理 ≤ **14**、动作 ≤ **16**、时间槽 ≤
**10**、标记 TTL ≤ **6**。

---

## 3. 输入用例结构

顶层对象的主要字段：

| 字段 | 含义 |
|---|---|
| `nodes` | 节点：`ingress` / `proxy` / `service` 三类 |
| `identities` | 调用身份名录 |
| `services` | 服务及其**租户标签** |
| `links` | 已登记链路：源/目的、`delay_min..delay_max`（离散步）、`failure_domain`、`carries`（可投递服务，`"*"` 为通配） |
| `policies.inbound` | 每个代理的多版本**入站校验规则**（身份, 租户, 标记, 目标, accept/deny） |
| `policies.outbound` | 每个代理的多版本**出站标记映射**（标记, 身份, 租户, 目标 → `set_marker`） |
| `initial_state` | 每个代理初始已安装的入站版本集合与唯一活动出站版本 |
| `requests` | 请求：身份、**原始租户**、初始标记、入口、目标服务、发射步、TTL、截止步、适用名单 `allow_nodes`、可选 `force_gen` |
| `actions` | 六类升级动作（见下） |
| `precedence` | 动作先后约束（二元组，校验无环） |
| `replay` | 复演定时事件、视界、可选单故障域窗口 |
| `plan` | 槽位数、视界、屏障最大等待槽、可选故障窗口与 `search` 设置 |
| `baseline` | 无合法表时用于冲突/失败定位的基线动作顺序 |

字符串 `"*"` 表示通配匹配。规则按**最具体优先**（具体约束数多者胜），因此一条兜底
deny 不会遮蔽同版本中更具体的 accept。

### 3.1 六类升级动作

| `type` | 语义 |
|---|---|
| `install_inbound` | 指定代理**额外**接受一个入站新版本；不改变已在途标记的原始发射标记 |
| `switch_outbound` | 把指定代理的活动出站映射切换为新版本（成为唯一活动版本） |
| `withdraw_inbound` | 撤下旧入站版本；**必须先有真实排空证明**，否则记 `withdraw_without_drain_proof` |
| `barrier` | 插入带代次的排空屏障，选择等待槽数并在槽末检查见证集合 |
| `pause` / `resume` | 暂停/恢复指定代理 |

---

## 4. 形式化语义（引擎关键约定）

- **原始租户不可变**：出站映射只能更换标记（`marker`），令牌的 `tenant` 字段从发射到
  投递永不改变。服务侧隔离检查比对的是**原始租户 vs 服务租户标签**。
- **TTL 为绝对期限**：令牌携带 `expiry_step = emit_step + ttl`，在第一个满足
  `step >= expiry_step` 的处理点丢弃。暂停排队、故障链路上滞留、环路绕行都会照常消耗
  TTL。
- **入站判定**：一个代理可同时安装多个入站版本；跨所有已安装版本，存在 accept 且无
  更具体 deny 才放行，否则按 `inbound_denied` / `inbound_no_match` 丢弃。
- **多链路分叉**：一个令牌在每条目标可承载的出链路上各派生一个分支，分支 ID 记录完整
  路径（如 `R1#root>L0>L1>L2`）。不同分支的回执相互独立，**不能覆盖或混写**；同分支重复
  投递记 `duplicate_delivery`，晚于截止步记 `late_delivery`，各自带可核验原因。
- **暂停**：暂停期间代理不接受也不转发新标记；到达的标记进入该代理的保持队列，只等待
  或到期。恢复时重新做入站校验/代次核对后再转发，期间到期的标记以 `ttl_expired` 丢弃。
- **代次与屏障**：每个屏障使全局代次 +1。屏障插入时快照“仍可能到达该代理”的在途标记
  集合作为**见证**；槽末检查时每个见证标记必须已终结（交付或丢弃），屏障才算完成并把
  该代理的最低开放代次（单调不减）推进到新代次。代次戳（或显式 `force_gen` 出生代次）
  低于最低开放代次的标记以 `stale_generation` 丢弃并记 `generation_reinjection`，从而
  **屏障完成后新事件无法重新注入已关闭代次**。
- **撤下的排空证明**：撤下动作执行时，若仍存在任何可能到达该代理的在途标记，即判定缺乏
  真实排空证明，动作置 `ok=false` 并产生违规，不会静默撤下。
- **故障域**：至多一个域有失效/恢复窗口。窗口内该域链路既不接受新标记，也把已在链路上
  的标记移入阻塞队列；恢复时按输入的伸缩时限（默认该域链路最小延迟）重算残余延迟，恢复点
  已到期的标记记 `ttl_expired_at_recovery`，**绝不把已丢弃/到期标记算作恢复成功**。

### 时间线压缩（如何只保留仍影响未来的在途状态）

仿真器是**事件驱动**而非逐槽空转：每一步处理完后，直接跳到下一个“有事发生”的步
（发射、到达、控制动作、故障边界、屏障检查或 TTL 到期之一）。一个仍影响未来的活动状态
被压缩为元组

```
(在途令牌: (token_id, link, arrive_step, expiry_step),
 每代理: 已安装入站版本集合 / 活动出站版本 / 暂停标志 / 最低开放代次)
```

已终结（交付或丢弃）的令牌立即从活动集合剪除，因此仅在死令牌上有差异的历史共享同一
“空在途”后续。TTL ≤ 6 还为任一令牌可占据的在途步数给出了硬上界。输出的 `compression`
字段给出视界步数、实际访问步数与跳过步数。

---

## 5. `replay` 输出

顶层包含：`overall_status`（`safe`/`violation`）、`earliest_violation`、`barriers`、
`receipts`、`drops`、`anomalies`、`violations`、`actions`、`timeline`、
`reachability`、`compression`。

- 每个屏障给出代次、插入/检查步、见证令牌、终结/未终结令牌及逐令牌 `fate`。
- `earliest_violation` 给出最短违规前缀；平局按“前缀长度 → 事件 ID 序列字典序 → 违规码
  → 事件 ID”裁决，并附 `tie_break_rule`。
- 必达性在视界闭合时核对：每个获准请求至少有一条在截止步内的一手回执，否则记
  `reachability_failure`。

---

## 6. `plan` 搜索方法与正确性边界

### 6.1 情形包络（固定计划必须对全部情形安全）

- **延迟情形**：默认 `extremes`，枚举每条链路取 `delay_min`/`delay_max` 的所有角点
  （中间延迟只会把到达步移入相同交错包络内部）；`search.delay_mode="grid"` 可枚举完整
  笛卡尔积，受 `scenario_cap` 限制。
- **故障情形**：默认 `fixed`（输入给定的唯一窗口）加无故障情形；
  `search.failure_mode="aligned"` 把同一窗口对齐到每个相关步边界，覆盖故障时刻不确定性。
- 完全相同的（延迟, 故障）角点去重。计划**不依赖运行时才能观察到的未来延迟**：屏障只能
  选等待槽数并在槽末检查。

### 6.2 搜索

- 在“同代理或先后约束”冲突图上求**连通分量**；不同分量的动作互不约束，可共享槽位，
  从而把跨分量笛卡尔积从搜索内层移到合并层。每个分量枚举锚定槽 0 的归一化局部表，合并
  时再枚举整体偏移，保留分量之间相对故障窗口的绝对时序语义。
- 硬约束：先后约束、同代理同槽互斥、屏障等待窗占用、pause/resume 配对。
- 目标按（停用代理总时长，发布步数，动作 ID 序列）字典序最小化，并带分支限界。
- **静态必达性预检**：用最短路（全最小延迟）计算每个请求与调度无关的最小可达步数；若连
  最短路径都超出 TTL/截止步，直接判 `structurally_unreachable`，不枚举任何发布表。

### 6.3 有界完备性（诚实的截断语义）

独立分量的偏移合并在病态输入上可能极大。搜索在 `component_budget`/`node_budget`
内保持**完备**；触顶时置 `search_truncated: true` 而**不**谎称已穷尽，并仍返回已找到的
最优可行表。结构性不可行输入由预检即时拒绝（实测上限规模 < 0.05s），可行上限规模在普通
笔记本 CPU 上数秒内返回。小拓扑下 `exhaustive_crosscheck` 独立暴力枚举全部合法表，对拍
DFS 最优解（见测试）。

不可行时：若基线本身违反排程约束，返回 `baseline_scheduling_conflict` 与最早冲突；否则
对基线仿真，返回最短隔离/必达失败前缀（等长取事件 ID 序列最小者）。

---

## 7. 六个样例与题目点名场景的对应

| 样例 | 覆盖场景 | replay 关键结果 |
|---|---|---|
| `01_safe_gray` | 正常灰度（安装→屏障→切换→撤下） | `safe`，两请求均必达，原始租户经多次改标后不变 |
| `02_leak_window` | 两个最终合法版本之间的**泄露窗口** | 最早违规 `isolation_violation`，ta 令牌被改标后投递到 tb 服务 |
| `03_ingress_swap_strand` | **入口更换导致旧标记滞留** | 无排空证明撤下 + 旧标记 `inbound_denied` 滞留 |
| `04_generation_split` | **同一状态在不同屏障代次有不同未来** | `force_gen=0` 的令牌在代次 2 后被 `stale_generation` 拒绝 |
| `05_ttl_failure_boundary` | **故障窗口结束恰逢 TTL** + 暂停保持/恢复 | `ttl_expired_at_recovery` 不计恢复成功；保持队列令牌恢复后投递 |
| `06_scenario_conflict` | **单情形最短序列无法共同使用** | 固定基线下故障窗口使 R1 必达失败 |

---

## 8. 复现步骤

```bat
REM 1) 校验全部样例
for %f in (examples\0*.json) do python -m solution validate %f

REM 2) 复演并查看最早违规前缀（以泄露窗口为例）
python -m solution replay examples\02_leak_window.json

REM 3) 规划固定发布表（安全样例应 feasible=true）
python -m solution plan examples\01_safe_gray.json

REM 4) 穷举对拍与全部断言
python -m unittest discover -s tests -v
```

## 9. 错误码速查

`FILE_NOT_FOUND`、`JSON_PARSE_ERROR`、`NOT_AN_OBJECT`、`MISSING_FIELD`、
`WRONG_TYPE`、`ILLEGAL_VALUE`、`DUPLICATE_ID`、`UNKNOWN_REFERENCE`、
`CARDINALITY_VIOLATION`（含 14/16/10/TTL6 上限）、`SEMANTIC_CONFLICT`（含先后约束环、
基线同槽冲突）、`UNKNOWN_COMMAND`。
