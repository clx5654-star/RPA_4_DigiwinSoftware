# E10 UI 自动化（e10_rpa）

## 定位（先读这个）

本目录是仓库 E10 接入策略的**补充通道**，只服务一种场景：

> **数据库（MIDDLEDB）和 OAPI 都覆盖不到、必须走界面的动作**——查询导出、
> 审核、打印、低频单据操作等。少量、低频、有人值守。

**不要用它扛批量数据**。数据接入的正路是 MIDDLEDB 直连 / OAPI / 适配器
（见 `docs/plans/erp-adapter-mapping-*.md`、`scripts/srm_po_sync.py`）。
量级参考：UI 通道当前只验证 23 行；一页容量及超过一页的能力尚未真机证明；
同步侧谈的是 4,257 / 11,126 个键。

## 文件

| 文件 | 角色 |
|------|------|
| `e10_query.py` | **E10 UI 适配器**：状态机 → 按名导航 → 查询方案/高级查询 → 抓网格 → xlsx |
| `rpa_core/` | **纯领域内核**：查询三态、网格指纹、窗口所有权、selector 唯一性、JSONL 证据；不依赖 Windows/UIA |
| `tests/` | 不启动 E10 的离线单元测试，重点防止旧网格假成功和误关用户窗口 |
| `selectors.json` | 界面字符串配置（组按钮/编辑器名/查询方案…）。改文案/换账套只动这里 |
| `e10_recorder.py` | **机制发现器 v2**：默认只附着一个 E10 进程，产出文本 + JSONL 双证据；记录事件窗口、点击前后控件、祖先作用域、UIA 能力和 selector 唯一性候选，**不作为回放生产线** |
| `e10_login.py` | **E10 登录入口**：启动/附着客户端、只读探测登录控件、秘密安全输入、单次登录提交、主界面/失败弹窗终态分类 |
| `e10_credentials.py` | **本机加密凭据库**：密码使用当前 Windows 用户的 DPAPI 加密；只输出档案元数据，不显示密码 |
| `rpa_submit.py` | **本机触发器**：提交、查询、列表、取消结构化任务；不接受任意命令或程序路径 |
| `rpa_submit_gui.py` / `dist/E10_RPA_Submitter.exe` | **图形任务提交器**：选择登录、请购创建或请购核对，填写白名单参数，查看状态和请求取消；不直接操作 E10 |
| `rpa_receiver.py` | **交互桌面接收器**：领取租约、运行白名单工作流、维持心跳并按 JSONL 证据回写终态 |
| `rpa_control/` | **任务控制面内核**：严格协议、白名单、SQLite 队列、artifact、fencing token、子进程证据判定 |
| `e10_purchase_requisition.py` | **采购请购写入与独立核对入口**：业务范围仅限“维护请购单”；严格读取一行 XLSX、输出 registry 语义计划；FRKTEST 可执行端到端新建并核对，也可按身份只读 verify |
| `e10_requisition.py` | **旧命令兼容入口**：仅转调 `e10_purchase_requisition.main()`，不得再放入 ERP 业务实现 |
| `rpa_core/steps.py` | **写步骤协议**：`precondition → locate → act → readback → expect → reconcile`；写前 intent、动作最多一次、不确定提交不重试 |
| `ops_log_*.txt` | 历史录制（机制知识的原始凭证） |
| `attic/` | 已废弃的"录制→修剪→回放"线（e10_player.py、flows/、探针）。保留供考古，勿再用 |

## ERP 业务模块边界

`e10_purchase_requisition.py` 只实现“采购管理 → 维护请购单”业务，包括请购输入、
保存、核对和该流程专用诊断。后续采购订单、收货、退货、审核等 ERP 业务必须建立独立的
同级模块或 workflow 包；可以复用 `rpa_core/`、安全协议和本模块已经验证的实现模式，
但不得通过向 `e10_purchase_requisition.py` 追加分支来开发新业务。

旧文件 `e10_requisition.py` 仅用于兼容已经存在的本地命令。新代码、测试、文档和调度配置
统一引用 `e10_purchase_requisition.py`。审计 workflow ID `e10.requisition.*` 保持不变，
以便历史 JSONL、请求登记和测试单据台账继续关联。

## 用法

```bash
python e10_query.py --check-state                    # 只读状态报告
python e10_query.py --desktop-check                  # 只读证明 RPA 与 E10 位于同一交互桌面
python e10_query.py --inspect-main                   # 只读主窗口控件转储
python e10_query.py --scheme 全部                    # 查询方案 → 抓网格 → xlsx
python e10_query.py --query 单号=3500-19080171       # 单条件高级查询
python e10_query.py --tree 采购管理 --func 维护请购单 --scheme 全部        # 换功能
python e10_query.py --explain                       # 只读selector唯一性诊断，不点击
python e10_query.py --probe-grid                    # 只读网格完整性探针（要求已有一个浏览窗）
python e10_recorder.py                              # 自动附着唯一的 E10 进程，Ctrl+C 结束
python e10_recorder.py --pid 12345 --redact-values # 显式 PID；业务输入也脱敏
python e10_login.py --explain                       # 只输出登录步骤，不接触 E10
python e10_login.py --inspect-login --report-dir runs\login_probe  # 只读探测；编辑框名称脱敏
python e10_credentials.py list                       # 只列凭据档案元数据，不解密或显示密码
python e10_credentials.py validate --profile ft_test_hr12  # 验证当前 Windows 用户可解密
python e10_login.py --execute-login-live --human-present   # 默认使用 ft_test_hr12 档案
python rpa_receiver.py explain                              # 只显示本机能力；不创建数据库、不触碰 E10
python rpa_submit.py submit --workflow e10.session.login --environment E10_FT_TEST
python rpa_receiver.py run-once --attended                  # 领取并执行至多一个任务
python rpa_submit.py status --job-id JOB-...                 # 查询任务与完整状态事件
python e10_purchase_requisition.py                           # 校验默认 XLSX、对齐最新录制、输出语义计划
python e10_purchase_requisition.py --validate-only           # 只读校验 XLSX，不接触 E10
python e10_purchase_requisition.py --desktop-check           # 只读桌面准入检查，不执行 E10 动作
python e10_purchase_requisition.py --inspect-open-form        # 一次性只读扫描；显示阶段/耗时，45 秒硬超时
python e10_purchase_requisition.py --execute-create-live --request-no REQ-20260918-001 --account-set FRKTEST --human-present --report-dir runs\requisition_create_e2e
python e10_purchase_requisition.py --verify-live --doc-no 3110-26090006 --account-set FRKTEST --human-present --report-dir runs\requisition_verify
python e10_purchase_requisition.py --build-test-ledger           # 从 runs/**/*.jsonl 派生测试单据台账，不接触 E10
python e10_purchase_requisition.py --build-campaign-report --campaign-id BASELINE-001
# campaign 真机样本还必须在动作前声明：--scenario-id S1 --expected-result SUCCESS --attest-no-intervention
python -m unittest discover -s tests -v             # 不接触E10的离线测试
```

## 本机任务控制面 v1（2026-09-22）

控制面把“谁要求做什么”和“哪个进程实际操作 E10”分开，现有 UI actuator 保持原位：

```text
用户/本地业务程序
  └─ rpa_submit.py（触发器：校验任务、复制 artifact、写入 SQLite）
       └─ state/rpa_jobs.sqlite3（WAL 持久队列、事件、worker、租约）
            └─ rpa_receiver.py（接收器：交互桌面准入、DPAPI 准入、原子领取）
                 └─ rpa_control/registry.py（固定白名单与固定 argv）
                      └─ e10_login.py / e10_purchase_requisition.py
                           └─ jobs/evidence/<job_id>/（子 JSONL 与终态证据）
```

### 职责与安全边界

- 触发器只创建结构化任务，不操作 E10。
- 接收器不含 UI selector，也不接受任务提供的 Python 路径、可执行文件、Shell 或参数数组。
- 注册表是唯一的“任务 → 程序参数”翻译点；子进程固定使用参数列表和 `shell=False`。
- 执行器不能仅凭退出码成功。它要求唯一 `run_finished`，并验证该工作流允许的终态；COMMIT
  还要求唯一 `write_intent`、唯一 `step_act(write_request_issued=true)`，且 intent 严格在 act 前。
- 密码、环境变量密码值和任意命令字段在协议层递归拒绝。任务只引用环境别名，不携带凭据。
- 本机提交事件同时记录调用者声明和实际 Windows 用户、进程、主机；未来 HTTPS 入口应把
  `requested_by` 替换为经过认证的服务身份。

生产白名单当前只有三项：

| workflow_id | 风险 | 固定程序 | 成功终态 |
|---|---|---|---|
| `e10.session.login` | `REVERSIBLE_WRITE` | `e10_login.py` | `AUTHENTICATED` |
| `e10.requisition.create` | `COMMIT` | `e10_purchase_requisition.py` | `SAVED_CONFIRMED` |
| `e10.requisition.verify` | `READ_ONLY` | `e10_purchase_requisition.py` | `CONFIRMED / NOT_APPLIED` |

`tests/fixtures/control_fake_workflow.py` 只用于离线验收，从未进入生产白名单。

### 环境别名：FT 不等于 FRKTEST

外部只能提交 `environment_profile=E10_FT_TEST`。注册表内部固定展开为：

```text
credential_profile                 = ft_test_hr12
login_account_set                  = FT
requisition_business_environment   = FRKTEST
dataset_epoch                      = FRKTEST-RESET-20260918
```

`FT` 是登录页账套，`FRKTEST` 是请购脚本现有的测试业务参数，两者禁止混为同一字段。测试数据库
再次重置时，必须先更新并评审 `dataset_epoch`，不能由任务 payload 自由覆盖。

### 任务协议和状态

协议是严格 JSON/dataclass，未知字段默认拒绝。示例：

```json
{
  "schema_version": 1,
  "job_id": "JOB-20260922-000001",
  "workflow_id": "e10.requisition.create",
  "workflow_version": "1",
  "environment_profile": "E10_FT_TEST",
  "risk": "COMMIT",
  "request_no": "REQ-20260922-000001",
  "idempotency_key": "e10.requisition.create:REQ-20260922-000001",
  "input": {"artifact_id": "FILE-000001", "sha256": "..."},
  "timeout_seconds": 900,
  "requested_by": "local-user",
  "created_at": "UTC ISO-8601"
}
```

状态固定为 `QUEUED / LEASED / RUNNING / SUCCEEDED / FAILED / UNKNOWN /
REQUIRES_HUMAN / CANCELLED`。SQLite 使用 WAL、`busy_timeout` 和 `BEGIN IMMEDIATE` 原子领取；同一
数据库同时只允许一个 UI 任务处于 LEASED/RUNNING。每次领取递增 `lease_generation`，所有 worker
更新必须同时匹配 `job_id + leased_by + lease_generation`，旧 worker 的迟到结果会被拒绝。

| 风险 | 安全失败/过期 | 已提交或结果不明 |
|---|---|---|
| READ_ONLY | 在 `max_attempts` 内允许租约恢复重排 | 失败，不冒充业务成功 |
| REVERSIBLE_WRITE | 登录提交前可安全失败 | `login_submit` 后为 `UNKNOWN`，禁止再次点击 |
| COMMIT | LEASED 且未启动可释放；RUNNING 过期不自动重排 | `REQUIRES_HUMAN`，`safe_to_retry=false` |

### 提交、查询和接收

```powershell
# 请购创建：提交时复制 XLSX 到受控目录并计算 SHA-256；不会立即操作 E10
python rpa_submit.py submit `
  --workflow e10.requisition.create `
  --environment E10_FT_TEST `
  --request-no REQ-20260922-000001 `
  --input-file "info\请购RPA写入测试.xlsx"

# 只读核对
python rpa_submit.py submit `
  --workflow e10.requisition.verify `
  --environment E10_FT_TEST `
  --doc-no 3110-26090001

python rpa_submit.py status --job-id JOB-...
python rpa_submit.py list --status QUEUED
python rpa_submit.py cancel --job-id JOB-...

# 图形提交器：双击 EXE
dist\E10_RPA_Submitter.exe

# 接收器必须在拥有 E10 与 DPAPI 凭据的登录用户桌面运行
python rpa_receiver.py run-once --attended
python rpa_receiver.py serve --attended --poll-seconds 2
# 等价的、更直观的常驻命令；窗口保持打开即持续接收，Ctrl+C 正常停止
python rpa_receiver.py start --attended
```

`serve`/`start` 是前台常驻模式，适合直接从 CMD 或 PowerShell 启动。启动后会先输出
`RECEIVER_STARTED`，空闲时输出 `IDLE` 状态并每 30 秒打印一次存活心跳；有任务时立即输出任务终态。
关闭控制台会终止接收器；空闲时可使用 `Ctrl+C` 正常停止，worker 会登记为 `STOPPED`。任务运行中
应先通过 `rpa_submit.py cancel` 提交取消请求并等待任务进入终态，不要直接关闭控制台。若要随 Windows
用户登录自动启动，可由任务计划程序执行同一命令，但必须选择“仅当用户登录时运行”，不能放入
Session 0 Windows 服务。

图形提交器与 `rpa_submit.py` 使用同一个 SQLite 队列和同一套白名单校验。打开界面不会启动或操作
E10；点击“提交任务”只会写入 `QUEUED` 任务，仍由独立常驻的 Receiver 领取。请购创建会要求再次
确认，并且只能选择 `.xlsx`；界面不提供密码、命令、脚本路径或任意参数入口。若需重新构建 EXE，
先安装 PyInstaller，再执行 `powershell -ExecutionPolicy Bypass -File .\build_submitter_exe.ps1`。

同一界面的 Receiver 控制区提供固定的三个安全动作：

- **开启/恢复**：只允许启动项目根目录下固定的 `rpa_receiver.py start --attended`；已经运行时仅把
  控制状态改回 `RUN`，不会启动第二个已知 Receiver。
- **挂起**：控制状态改为 `PAUSE`，Receiver 不再领取新任务；已经运行的子 RPA 继续到明确终态。
- **安全终止**：控制状态改为 `STOP`；空闲 Receiver 在下一轮轮询退出，已有任务时等任务结束后退出。
  GUI 不提供强杀按钮，避免中断已发出但结果未明的登录或 COMMIT 写请求。

界面显示 Receiver 的 worker 状态、PID、当前任务与最后心跳。固定控制文件为
`state/receiver_control.json`，Receiver 进程日志为 `jobs/process_logs/receiver_current.log`。控制文件
只接受 `RUN / PAUSE / STOP` 三个值，字段损坏或出现未知值时 Receiver 失败关闭为 `NOT_READY`，不领取任务。

运行路径：

- SQLite：`state/rpa_jobs.sqlite3`
- 受控输入：`jobs/input/<artifact_id>/`
- 每任务证据：`jobs/evidence/<job_id>/`
- 证据至少包含：`stdout.log`、`stderr.log`、`receiver.jsonl`、`artifact_manifest.json`、
  `result.json` 和 `child/*.jsonl`

上述运行目录已加入 `.gitignore`。XLSX 提交时拒绝 UNC、`..`、非法扩展名；复制后及执行前都验证
SHA-256，receiver 从不读取任务给出的任意绝对路径。

### 为什么不能运行在 Session 0

E10 UIA、鼠标、键盘、DPAPI 当前用户凭据都依赖实际登录用户的 `WinSta0\\Default` 桌面。
Windows 服务的 Session 0 看不到该桌面，也不能可靠控制 E10。当前部署方式必须是 Windows 任务
计划程序“仅当用户登录时运行”，使用拥有 E10 与 `ft_test_hr12` 的同一 Windows 用户启动
`rpa_receiver.py serve --attended`。锁屏、断开的 RDP、非活动 Session 或 DPAPI 解密失败时，worker
标记 `NOT_READY` 且不领取任务，不能伪装成业务失败。

### 向中央调度迁移

未来中央 HTTPS 服务只需复用 `TaskRequest` JSON、状态集合、artifact 哈希和租约/fencing 语义；
部署机改为主动拉取中央任务并回传事件。白名单、环境映射、子进程证据判定和 E10 actuator 无需
搬迁。中央化之前仍缺少：服务端身份认证/TLS、多虚拟机路由、artifact 下载签名、集中凭据轮换、
worker 安装升级和人工处置界面。本机 v1 也不支持 Session 0、自动 Windows 登录、强杀 E10、
自动丢弃草稿或 UNKNOWN 后盲目重试。

## E10 登录流程（2026-09-22.2）

登录模块是独立工作流 `e10.login`，不属于请购业务。执行顺序固定为：交互桌面准入 →
启动或附着 E10 → 唯一定位控件 → 输入与检查 → 登录按钮只调用一次 → 根据主窗口或提示
弹窗分类终态。开发阶段账套被硬限制为 `FT`；若 E10 已经处于主界面，由于无法从登录页证明
当前账套，流程会安全拒绝，不会把“已登录但账套未知”冒充成功。

密码不支持 `--password`，避免出现在命令历史和进程列表。默认凭据档案是 `ft_test_hr12`：
用户名为 `HR12`、账套为 `FT`，密码使用 Windows DPAPI 当前用户范围加密保存在
`.secrets/credentials/`；目录及文件 ACL 只允许创建它的 Windows 用户和 SYSTEM 访问，且整个
`.secrets/` 已由 `.gitignore` 排除。密码内容不会写入 JSONL、截图或 selector；探针也会对所有
编辑框名称脱敏。该档案换到另一 Windows 用户或另一台未迁移 DPAPI 密钥的机器后无法解密。

不使用凭据档案时仍可显式传入空的 `--credential-profile`，再使用隐藏输入或
`E10_RPA_PASSWORD` 环境变量；这是人工诊断回退路径，不是部署默认值。生产部署应进一步替换为
集中式凭据库适配器和轮换机制。命令行用户名或账套与所选档案不一致时拒绝覆盖；无论使用哪种
来源，账套不是 `FT` 都会在任何登录点击之前终止。

密码目标必须唯一且 UIA `IsPassword=true`；用户名、可选账套和登录按钮必须按语义名称或
非数字 AutomationId 唯一命中。0 个、多个、零矩形、未知窗口均停止。登录请求发出后，失败
弹窗返回 `LOGIN_REJECTED`，无法证明结果返回 `LOGIN_RESULT_UNKNOWN`；两者都不自动二次点击，
避免账号锁定。首次真机使用前先让 E10 停在登录页执行 `--inspect-login`，核对候选控件后再执行。
2026-09-22 首次真机只读探针确认 `txtUserName / txtPwd(IsPassword=true) / btnOK /
cboCompanyName`，当前仅标记为单会话候选；完成客户端跨重启复验后才能升级为
`restart_verified`。

2026-09-23 启动序列只读探针确认 E10 冷启动可经历：`更新程序`（约 5.5 秒）→ 长时间无窗口 →
`LoadForm`（约 30.2 秒，类 `WindowsForms10.Window.8.app.0.2004eee`）→ `登录`（约 31.2 秒）。
`LoadForm` 已作为启动阶段已知 transient 写入 `selectors.json`；登录流程保留 120 秒整体启动等待，
因此能覆盖约 40 秒的正常慢启动。只有 `欢迎 / 更新程序 / LoadForm` 等已登记过渡窗口允许继续等待，
其他未知窗口仍立即失败关闭。启动等待的窗口状态变化写入 `startup_wait_observations`，避免再次只得到
缺少窗口身份的 `WINDOW_NOT_FOUND`。

### 未来定位：会话级纠错恢复入口

`e10_login.py` 后续同时作为各业务 RPA 的**会话级纠错备选方案**。当某个业务流程无法可靠
判定 E10 当前界面，且常规的窗口还原、前台提升、作用域重解析均失败时，上层编排器可进入
“恢复到已知主界面”流程：记录失败现场 → 安全退出 E10 → 重新启动客户端 → 调用登录流程 →
确认唯一主界面。业务模块只提出恢复请求，不自行复制登录动作或直接猜测当前界面。

这不是普通步骤重试，也不表示现在已经实现自动退出。启用前必须同时证明：没有已发出但结果
未知的写请求、没有需要保留的未保存单据或草稿、目标 E10 进程与当前交互桌面和执行器唯一对应。
任一条件无法证明就停止并交人工，禁止通过杀进程掩盖 `UNKNOWN`，也禁止恢复后从写操作中段
继续执行。恢复成功后只能从业务流程定义的安全起点重新开始，并继续受业务请求号、防重闸和
单次提交规则约束。默认应先尝试可验证的正常退出；强制结束进程只能作为显式授权且有证据记录
的最后手段。

该恢复能力未来应由独立的会话编排/恢复层调用；`e10_login.py` 继续只负责“启动或附着、登录、
判定登录终态”，不承载采购、请购等业务操作。

## 交互桌面准入闸（2026-09-18.7）

所有会读取、聚焦或操作 E10 控件的请购与查询入口，在调用 UIA 前都必须证明以下条件：当前
进程位于 `WinSta0\\Default`，当前 Session 处于活动状态，输入桌面是 `Default`，并且当前
桌面上恰有一个属于同一 Session 的可见 E10 主窗口。最小化不影响准入，后续窗口恢复逻辑会
负责还原；被其他窗口遮挡也不影响准入。锁屏、断开的 RDP Session、Windows 服务/Session 0、
Codex 隔离桌面、其他用户 Session、零个或多个 E10 主窗口都会在任何点击和键盘输入之前立即
拒绝，并写入终态 JSONL，不再用长超时假装“找窗口”。

因此运行前先执行：

```bash
python e10_purchase_requisition.py --desktop-check
```

只有输出 `status=READY` 才可启动真机流程。任务计划程序必须选择“仅当用户登录时运行”，并以
已登录 E10 的同一 Windows 用户启动；不要选择“无论用户是否登录都运行”，不要从 Windows
服务调用。RDP 运行期间不要锁屏或断开会话，人工在场流程也不要切换用户。准入通过只证明启动
时刻的桌面关系，运行期仍由现有前台监视、窗口恢复、唯一性断言和失败即停共同保护。

默认不再因为看不到主窗口就自动启动第二个 E10。确需由 RPA 启动客户端时，必须在已经证明
`WinSta0\\Default` 可交互后显式增加 `--allow-start-e10`；该开关不会绕过锁屏、隔离桌面或
Session 校验。单实例锁同时占用 `Global\\HNF_E10_RPA_EXECUTOR` 与旧的
`Local\\HNF_E10_RPA_EXECUTOR`，防止不同 Session 的新旧执行器并发发送输入。

## 请购工作流透明度（2026-09-18）

`rpa_core/workflows.py` 是请购步骤元数据的单一来源，按
`end_to_end / fill / resume_fill / resume_open_item / resume_quantity / verify`
分组。它只描述现有语义动作，不搬迁 UI helper，不改变已真机验证的 selector 和 actuator
调用顺序。`semantic_plan()`、控制台计划、JSONL 的 `details.step` 和离线顺序断言均使用其中的
标准名称；既有名称 `open_new`、`save_requisition_once` 保持不变。

每步计划显式报告 `risk / modifies_business_data / retryable / opened_windows /
closed_windows`。resume 路径按各自计划的合法子序列验证，不再错误要求它们从完整新建流程的
第一步开始。成功保存还要同时满足：`write_intent` 恰好一次、
`step_act(write_request_issued=true)` 恰好一次，且 intent 严格在 act 之前。

仓库是条件步骤：输入没有“仓库”时，JSONL 必须记录
`step=verify_warehouse status=SKIPPED reason=not_in_input_e10_auto_default`，并保留 E10
自动默认仓库的整行读回；输入明确提供仓库时，该步骤变为 required。由于当前尚未实现显式
仓库选择，这种输入会在触碰界面动作前失败，绝不静默使用默认仓库。

`--verify-live --doc-no` 是独立只读入口，只允许打开浏览窗、按单号查询、读取状态区和清理
本次窗口，不包含新建、填写、校验、保存或草稿恢复。状态映射复用既有词汇：精确唯一
`FOUND -> CONFIRMED`，有明确零笔信号的 `EMPTY -> NOT_APPLIED`，查询失败、证据不足或不唯一
均为 `UNKNOWN`；核对源固定记录 `E10_UI_TEST_DATABASE`、`external=false`。UNKNOWN 不得冒充
“单据不存在”。

2026-09-18 真机补充：左侧箭头只控制子树展开；真正让右侧渲染采购流程图的是点击
`TreeItemControl(Name="采购管理")` 这一行。若左树保留了旧滚动位置、该节点为零矩形，流程先
要求同名节点唯一，再用其 `ScrollItemPattern.ScrollIntoView()` 物化，重新验证非零矩形后点击。
只有右侧已经切换而“维护请购单”仍未实体化时，才使用右侧唯一水平 `ScrollPattern`；打开浏览
窗后恢复原百分比。全程不使用录制坐标。证据事件 `navigation_state/navigation_scroll` 记录实际
模式、滚动前后矩形或百分比。

顶层业务窗口等待支持受控恢复：仅对“精确标题 + 当前 E10 PID + 唯一 HWND”执行
`SW_RESTORE`、移回虚拟桌面和临时 topmost 前置；随后立即取消 topmost，并要求窗口确实成为
前台且重新通过实体化检查。多命中、错 PID、隐藏内部窗和零矩形幽灵窗仍拒绝执行。恢复事件写入
JSONL `window_recovery`；等待关闭时最小化窗口仍算存在，不能冒充已关闭。该策略不点击任务栏坐标。

独立 verify 已对 `3110-26090001` 真机得到
`FOUND/MATCHED -> CONFIRMED`，观测 1 行与状态区 `(共1笔)` 一致，完整性为 `VERIFIED`；
工作流 `2026-09-18.2` 的证据为
`runs/requisition_verify/2ef97793b8b848489645fd2fa80d0d4a.jsonl`。结果截图中也可见唯一单号
`3110-26090001`。此前对 `3110-26090006` 和 `3110-26090005` 得到
`EMPTY/ZERO_ROWS -> NOT_APPLIED`，状态区原文均含 `(共0笔)`，证据分别为
`runs/requisition_verify/2256425ec56949519c1e4012ab60d418.jsonl` 和
`runs/requisition_verify/67c8578c73bc445c8e624ce09f3a7d78.jsonl`。

2026-09-18 陌生初始界面完整 E2E 最终通过：工作流 `2026-09-18.5` 从当前主页面纠正左树
虚拟化和右侧流程图滚动，创建并保存 `3110-26090002`，最终
`SAVED_CONFIRMED`。保存前读回单据类型 3110、申请人王中前、需求日期 2026-10-17、品号
`21302050002R`、数量 5000，E10 自动仓库为 `郴州电子仓H`；保存后按单号查询得到
`FOUND/MATCHED`。证据为
`runs/requisition_create_e2e/3184024fc24b4973909291cf1b6002c8.jsonl`。其中
`write_intent` 与 `step_act(write_request_issued=true)` 均恰好一次且顺序正确。本次还自然触发
一次被其他窗口遮挡的编辑窗恢复：`BRING_TO_FRONT` 后前台确认成功，临时 topmost 已解除。

端到端写入和独立 verify 会在运行前打印完整计划，并由不调用 UIA 的 daemon 线程输出当前
步骤心跳。心跳只说明 Python 进程仍存活、当前调用尚未返回且没有完成证据，不构成业务成功
证据，也不会触发重试。

`--build-test-ledger` 从历史 JSONL 派生 `runs/requisition_test_ledger.json`。范围包括已确认
单据、已发出写请求但结果未知的运行、预分配未保存草稿和 resume 接管记录；原始证据路径保留，
清理状态不得覆盖创建证据。历史日志没有输入文件路径/哈希时明确列在 `evidence_gaps`，不伪造。
证据派生与人工登记现已拆开：负责人、保留/作废决定、人工抽检结果只能写入
`runs/requisition_manual_registrations.json`，随后按单号合并；代码不再生成这些字段。任何
`persisted=true` 单据缺负责人或决定都会使台账校验失败。台账不会自动删除、作废或撤销 E10
单据，清理由人工登记完成。

只有日志 `step` 标注、registry、控制台计划、心跳、只读 verify、台账或离线测试变化时，既有
写入证据仍可作为 actuator 基线；一旦 selector、动作顺序、helper 调用关系、保存位置、reconcile
编排或风险策略改变，必须在 FRKTEST 人工在场时重新执行一次完整 E2E，并登记新单号及清理计划。

## 请购单首条写流程（2026-09-17）

输入文件 `info/请购RPA写入测试.xlsx` 已与录制
`runs/recordings/recorder_v22_smoke/ops_log_20260917_152838.jsonl` 对齐：单据类型
`3110`、申请人、需求日期、品号和请购数量五项输入均在录制中出现；录制包含一次“保存”，
保存后界面出现单号 `H250505007`，随后编辑窗在没有“未保存”确认的情况下关闭。

当前已提供仅限 FRKTEST 且要求 `--human-present` 的端到端入口：从 E10 主页面进入采购管理，
打开维护请购单、新建、填写、直接单击普通“校验”、记录 write intent、单击一次“保存”，再在
E10 测试数据库的浏览窗口中按预分配单号精确查询。测试账套数据不会进入 MIDDLEDB，因此此处
核对源明确记为 `E10_UI_TEST_DATABASE`、`external=false`，不能解释为生产环境的外部核对。
保存结果未知时不会再次点击保存。流程结束只关闭运行开始后新出现的带标题 E10 窗口，回到主页面。

2026-09-17 真机探针结论：旧版在 E10 窗口上调用无界
`pywinauto.window.descendants()`，三次均超过 45 秒；现已改为 Win32 精确找窗后，使用
`uiautomation.WalkTree` 做 16 层 / 800 节点双重有界遍历。实机连续三次分别约 8、10、18 秒
完成，证据为 `runs/requisition_probes/requisition_form_probe_20260917_160842.json`、
`...160928.json`、`...161038.json`。最终证据含 193 个节点，确认了
`SelCtrDOC_ID`（单据类型）、`SelCtrSTAFF_ID`（申请人）、`UDF_dtpUDF041`（需求日期）和
`grdREQUISITION_D`（请购明细）四个稳定语义作用域。表头两个查询控件各有两个无名称按钮，
只能在各自作用域内要求“恰好两个并取最左查询按钮”（实机反例已证明最右按钮不会打开查询窗），
仍需跨重启复验；绝对矩形继续只作诊断。
最初探针只能看见“品号/请购数量”等列，不能把“默认仓库库存”列误当成仓库选择；后续端到端
实跑已从整行读回确认本测试数据由 E10 自动带出仓库，显式仓库选择仍未实现。

2026-09-17 17:31–17:32 端到端真机通过：创建并保存单号 `3110-26090005`，查询结果
`FOUND/MATCHED`，E10 状态区自报“共1笔”，编辑窗和浏览窗均关闭。保存前表体读回品号
`21302050002R`、数量 `5000`，E10 自动带出仓库 `郴州电子仓H`；由于 XLSX 没有“仓库”列，
当前模板只记录该自动默认值，不支持按输入显式选择仓库。JSONL 证据为
`runs/requisition_create_e2e/199da7f5b7b24643a40af6af08886111.jsonl`；其中 `write_intent`
严格早于唯一的 `step_act/SENT`，最终 `SAVED_CONFIRMED`、`safe_to_retry=false`。
最终整合版 `2026-09-17.4` 又独立创建并确认单号 `3110-26090006`，证据为
`runs/requisition_create_e2e/4354b030aa074ec9bb8883b7fd56a979.jsonl`；其 `cleanup` 为 `OK`，
运行开始后没有遗留带标题的 E10 窗口。

`2026-09-18.5` 新增对 E10 品号查询“隐藏壳 → 可见窗口”句柄切换的有界观察；隐藏壳不会被
强制显示，也不会在等待期内被提前当成永久失败。中断恢复还识别表头完全匹配、row 0 品号为空且
数量为空/零的 `header_only_with_blank_row` 草稿，只允许选择“不保存”关闭。任何非空异值仍拒绝
代替用户丢弃。

`rpa_core/steps.py` 已落地可逆写协议：写请求前必须记录 intent；`act` 没有重入路径；点击后
无论读回超时还是 UI 提示不可信，都只进入 `CONFIRMED / NOT_APPLIED / UNKNOWN` reconcile，
`UNKNOWN` 时 `safe_to_retry=False`。单纯 UI“成功”不能冒充外部核对。

## 稳定性基线与防重闸（2026-09-18.6）

写流程现在必须取得一个**外部业务请求号**：来自 XLSX 可选列“业务请求号”或 CLI
`--request-no`；两处同时存在但值不同会在触碰 E10 前拒绝。原内容哈希已改名为
`payload_fingerprint`，只用于判断“同请求号是否仍是同一载荷”，不再冒充幂等键。
`runs/requisition_requests.json` 按 `campaign_id + request_no` 追加记录状态迁移：

- `CONFIRMED` + 同载荷：直接返回既有单号和证据，不打开 E10、不再次保存；
- `PENDING/UNKNOWN` + 同载荷：只允许按原单号走只读 reconcile，禁止保存；
- `NOT_APPLIED` + 同载荷：只有显式 `--retry-not-applied` 才能开始新 attempt；
- 同请求号但载荷不同：拒绝覆盖或合并。

新 attempt 在保存按钮调用之前先写 `PENDING`；`write_intent` 记录 request_no 与
payload_fingerprint。FRKTEST 的回查仍是 `UI_REQUERY/external=false`，不是数据库外部核对。

JSONL 已升到 schema v2，历史 v1 保留且读取兼容。每个新运行记录 campaign_id、源码与
selectors 总哈希、客户端 PID/进程启动 FILETIME、动作前窗口快照、步骤/等待耗时、闭合失败码、
纠错计数和人工干预声明。前台监视线程只调用 Win32 读取 API，不调用 UIA、不改变焦点；它只在
E10 曾成为前台后统计其他进程抢占前台的次数。任何正常、失败或中断运行都必须以
`run_finished` 收尾；报表把缺终态的历史文件单列为数据质量错误。

campaign 运行必须同时带 `--campaign-id <ID> --attest-no-intervention`。汇总报表把 verify、
fill/discard、e2e 分开统计，另列失败码直方图、等待耗时 P50/P95、无终态、无声明样本、代码哈希
一致性、假成功与重复真实 `step_act` 两条红线。UI 回查不能把假成功自证为 0；保存样本仍必须由
人工逐张核对关键字段并提供 E10 导出/打印证据。

当前**尚未进入基线 campaign**。历史四张单据 3110-26090002/0003/0005/0006 的人工登记仍空；
其中 0005/0006 的创建证据为 `SAVED_CONFIRMED`，后续 verify 却为 `(共0笔)/NOT_APPLIED`。
在负责人、处置决定、人工抽检和该矛盾解释写入独立人工登记文件前，`--build-test-ledger` 会按
设计返回 `INVALID`，不得冻结代码、跑 20+5 样本或根据历史直方图预设 top1。

2026-09-17 品号选择实机补充：在表体品号编辑器内直接写值并回车不会可靠回填；快捷菜单明确规定
F2=`查询品号`、F4=`查询资产类品号`。此前误发 F4 是自动化实现错误，不属于录制流程。
`查询资产类品号` 及其“未匹配，是否应用当前输入值”提示已加入明确拒绝清单。生产路径改为：
通过唯一 `SelCtrITEM_ID_CODE` 作用域激活品号编辑器，清除并读回任何残留值，再向该编辑器发送
F2，且只接受标题精确为 `查询品号` 的窗口；全程不保存屏幕坐标。
查询结果不仅要求品号值精确唯一，还按结果单元格行号选择同一行的 `选定 row N`，之后才允许
点击确定。错误的资产类查询只能通过显式 `--dismiss-unexpected-item-dialog` 点击 `btnCancel`
恢复，不能选择“应用当前输入值”。

数量列激活后会出现稳定作用域 `colREQUISITION_QTYSelectWindowEditor`。E10/UIA 会把其中同一个
WinForms 文本框同时暴露为“有原生 HWND”和“无原生 HWND”的两个重叠 EditControl；这不是两个
业务编辑器。实现只在该稳定作用域内取唯一带原生句柄的 `EDIT` 子控件，数字型 AutomationId
仍不进入 selector 配置。

“校验”按钮悬停出现的是不可点击的文字说明，不是下拉菜单；生产路径只对唯一的“校验”按钮
执行一次直接单击，然后检查有标题的新提示窗口及关键字段读回。无标题 tooltip/阴影窗口不作为
业务错误。

## 录制诊断器 v2

> 2026-09-17 安全修复：v2.0 曾把全树 selector 扫描、祖先/Pattern 读取放进默认高频路径，
> 且低级钩子事件转发的 64 位声明不完整，可能破坏 PowerShell/桌面的键鼠输入。v2.2 默认彻底
> 不安装全局钩子，改用只读 Win32 状态轮询；高成本 UIA 探测也改为显式开关。请勿在 E10
> 业务录制中开启下述两个深度探测参数。

- 默认只录制唯一的 E10 进程，拒绝在多个 E10 进程之间猜测；只有显式
  `--all-processes` 才恢复全局记录。
- 鼠标事件先冻结事件瞬间的 `(PID, HWND, 标题, 原生命中 HWND)`；点击前目标来自短时悬停
  缓存并带 `pre_target_quality`，拿不到时明确写 `unavailable`，不再把点击后的控件冒充点击前目标。
- 默认轻量模式只读取目标、焦点、新开/关闭窗口和浅层控件属性；点击、拖动、右键和双击次数
  均进入 JSONL。输入源使用 `GetAsyncKeyState` 只读轮询，不拦截、不修改、不转发 Windows
  输入链。安全模式不记录鼠标滚轮；需要横向滚动时请拖动滚动条，录制器会记为 `DRAG`。
- 控件身份包括 RuntimeId、实体矩形和非数字 AutomationId。矩形只标作
  `diagnostic_only`；数字型 AutomationId 标作 `session_dynamic`，不会生成 selector 候选。
- 高成本探测默认关闭：`--deep-control-probe` 才读取祖先链和 UIA Pattern；
  `--probe-selectors` 才在事件窗口作用域内做 0/1/多命中全树扫描。扫描达到节点上限时只能标
  `UNVERIFIED_SCAN_TRUNCATED`，不得宣称唯一。`UNIQUE_IN_CURRENT_SCOPE` 仍只是当前会话候选，
  必须经过客户端重启复验才能进入生产配置。
- 输入值经过约 1 秒静默期归并后记录读回结果；UIA `IsPassword`、登录/密码语义始终脱敏，
  `--redact-values` 可让普通业务输入也只保留长度和摘要。
- 安全轮询模式不会截获任何快捷键，因此不再要求录制时使用步骤标记热键；流程边界由时间顺序、
  窗口变化和输入读回整理为 `precondition → locate → act → readback → expect`。

已知边界：点击前目标依赖短时悬停缓存；快速移动后立即点击时可能只有点击后目标，此时证据会
明确降级。UIA 未公开的自绘控件仍只能得到容器级身份。selector 唯一性扫描只覆盖当时已实体化
的 UIA 树，虚拟化未展开项不在扫描范围内；跨重启稳定性仍须多次真机复验。

## 已确证的界面机制（来自录制，写进内核行为）

- **模块组切换**：左栏树只显示当前组的模块；切到"集团运营管理系统"组才有业务模块。选中的组没有按钮（渲染为 Group 标签）。
- **高级查询窗口在 UIA 树里不是顶层**：挂在浏览窗口名下，附加需三层匹配（精确→前缀→内嵌后代）。
- **字段下拉**：全新窗口字段区已有 Edit"数据编辑器"（同名取最左）；点它下拉即开，常用字段在首屏。
- **值编辑器**：点值区 Edit（同名取最右）激活；瞬态，二次点击会关掉。
- **输入三通道**：真实键入 → WM_SETTEXT（按矩形匹配 EDIT 子窗口，实测有效，绕开 SendInput 封锁）→ UIA SetValue；每通道读回验证（ValuePattern/Legacy，勿用 window_text——它返回控件标签）。
- 数字型 **AutomationId 是会话级 HWND**，禁止作生产定位依据；`btnQuery`、
  `menuGourpControl` 暂为 candidate，完成三次客户端重启复验后才能升为
  `restart_verified`。
- 查询行内两个同名编辑器暂按已确证的水平角色区分；该策略只允许用于单条件
  查询。多条件必须先解决“当前条件行”作用域，未复验前拒绝执行。
- 长列表虚拟化：未滚入视野的条目 rect=(0,0,0,0)，一律视为不存在。

## 边界（刻意保守）

- 只读查询线：**不进入单据编辑窗**。出现未知窗口（可能有未保存数据）一律中止交人工。
- 运行前已有浏览/查询窗口时拒绝执行，不自动关闭；结束时只按本次创建的
  `(PID, HWND)` 清理窗口。
- 查询结果为 `FOUND / EMPTY / FAILED` 三态。FOUND 必须有提交确认、内容刷新、
  稳定快照和业务字段命中；EMPTY 必须拿到 E10 明确的 0 笔状态，超时不能冒充空结果。
- 人工登录（不自动填凭据）；依赖前台焦点；有人值守设计，不是无人值守 RPA。
- 录制器默认只读轮询唯一 E10 进程，不安装全局键鼠钩子；登录/密码输入仍强制打码。
  它仍是临时诊断工具，只在需要探索时启动，勿长期挂机。

## 验收记录

- 2026-09-15：`--scheme 全部` 全流程通过，产出 query_result_*.xlsx（13 列 × 23 行真实数据）。
- 2026-09-16 16:49：重构后 `--query 单号=3500-19080171` 全流程通过——虚拟化下拉滚动
  选字段、条件行身份锁定、三通道填值、指纹判刷新全部生效；结果 FOUND/MATCHED，
  xlsx 恰好 1 行且单号精确命中；JSONL 证据 runs/6f3001ff7fd549c58b1daefaf653c44a.jsonl。
  离线域层测试 13 项全过。

## 2026-09-16 正确性基线

- 原始日志回放已退出生产路径。
- 修复旧网格“数量连续两次相同即成功”的假成功竞态，改用内容指纹与目标字段终检。
- selector 多命中一律失败并输出候选，不再取 `hits[0]`。
- 新增 JSONL 运行证据（run_id、workflow/selector版本、三态结果、是否修改业务数据、
  是否可安全重跑）。
- 离线测试覆盖：旧结果碰巧包含目标仍失败、同单元格数但内容变化可识别、明确零笔、
  selector 0/1/2 候选、精确窗口所有权和报告序列化。

尚待真机验收：网格状态栏笔数文本、`btnQuery/menuGourpControl` 跨三次客户端重启稳定性、
网格滚动前后 DataItem 集合与 E10 自报笔数是否一致。

## 路线记录：为什么回放线进了 attic/

"录制→人工修剪→回放"（e10_player.py）经实战检验后废弃，原因（外部评审确认）：
录制产物是原始输入事件而非操作意图，修剪成本≈手写脚本；定位键（rect 接近度/
automationid）随会话漂移；与语义内核的状态分类器互斥（单据窗被判 unknown 导致
自锁）；日志格式（repr+正则）非序列化格式。它带来的机制知识已全部沉淀进本内核
与上方"已确证机制"清单——这是它最好的归宿。

## 身份链与稳定性门（2026-09-18.8）

本节覆盖旧版关于“按裸单号核对”和“Global/Local 锁自动兼容”的描述。

- 单实例锁默认失败关闭：`Global\\HNF_E10_RPA_EXECUTOR` 返回 183 表示已有执行器；返回 5 记录 `MUTEX_NAMESPACE_DENIED` 并拒绝。只有显式 `--allow-readonly-lock-degradation` 的 READ_ONLY 流程可以仅持有 Local 锁；COMMIT/写流程使用该开关也会在 UI 动作前拒绝。
- `failure_code` 只属于 `run_finished`；等待超时、导航回退等过程事实使用 `observation_code`。报表分别输出终态失败直方图和观测计数。
- FRKTEST 写流程必须同时提供 `--request-no` 与 `--dataset-epoch`。稳定身份键为 `(dataset_epoch, request_no)`；请求号写入请购单“备注”字段，保存后按备注精确查询，并要求结果网格中预期单号恰好一行。业务 `payload_fingerprint` 不包含该注入字段。
- `identity_strength` 仅允许 `REQUEST_FIELD_MATCH / CONTENT_MATCH / DOC_NO_ONLY`。裸 `--doc-no` 核对即使命中也只能返回 `UNKNOWN`，因为测试库会重置并重用单号；强核对必须同时传 `--request-no` 与 `--dataset-epoch`。
- 台账人工登记键改为 `dataset_epoch::request_no::run_id`；历史证据使用 `legacy::run_id`。相同 `doc_no` 对应不同身份时校验失败，不再按裸单号合并。`--scaffold-manual-registration` 只补 null 槽位，绝不覆盖人工内容。
- 每次运行在首次 UI 动作前记录场景快照。快照中已存在未知/错误/警告窗时，campaign 样本记为 `SAMPLE_INVALID`；运行中才出现的弹窗仍计为 RPA 失败。无快照的历史样本只能记为未定性。
- 启动时对同一稳定身份扫描无 `run_finished` 的运行：registry 为 PENDING/UNKNOWN 时只允许 reconcile；没有 registry 记录时，只能在输入文件哈希及 payload 一致后检查本轮草稿，并复用严格草稿分类器决定是否“不保存”关闭，证据事件为 `startup_reconciliation`。

两条红线必须分开报告：

1. 同一 run 内 `step_act(write_request_issued=true) <= 1`，任何身份强度下都可证明。
2. 跨 run 无重复落库，只有 `REQUEST_FIELD_MATCH`（或未来 DB/OAPI 外部身份）才能证明；`CONTENT_MATCH`/`DOC_NO_ONLY` 必须输出 `UNPROVABLE`，不得显示为 0。

当前 Alpha 探针结论：`selCtrREMARK`（备注）作用域可写、可读回、可恢复原值，并可作为高级查询字段；探针全程未保存。证据为 `runs/correlation_probe_20260918/e0c3d980a2174369937d22033b23d3b2.jsonl` 与同目录 JSON 报告。保存后的强身份闭环仍须在历史单据人工处置完成后，用唯一一张新单 E2E 验收。

```bash
python e10_purchase_requisition.py --execute-create-live --request-no REQ-NEW-001 --dataset-epoch FRKTEST-RESET-20260918 --account-set FRKTEST --human-present
python e10_purchase_requisition.py --verify-live --doc-no 3110-26090001 --request-no REQ-NEW-001 --dataset-epoch FRKTEST-RESET-20260918 --account-set FRKTEST --human-present
python e10_purchase_requisition.py --scaffold-manual-registration
python e10_purchase_requisition.py --build-test-ledger
```

2026-09-18 操作者确认历史测试单 0002/0003/0004/0005/0006 已全部删除，后续查询不会再命中。稳定登记槽已逐 run 记为 `DELETE / DELETED_CONFIRMED_BY_OPERATOR`；历史重号因已全部退出当前数据集而不再阻塞生命周期台账，`--build-test-ledger` 已恢复为 `valid=true`。这不会追认历史裸单号为稳定身份：旧 campaign 的跨 run 红线仍为 `UNPROVABLE`，且其中一条无终态样本仍使旧 campaign 报告保持 `INVALID`。新的身份链与基线必须使用全新的 `dataset_epoch / request_no / campaign_id`。

## v3 P0 判据

- `FOUND` 在返回前检查 E10 自报笔数：自报数大于当前观测行数时返回
  `FAILED/INCOMPLETE_GRID`；无法取得自报数时允许 `FOUND`，但证据必须标记
  `completeness=UNVERIFIED`，不得解释为完整结果。
- 当前只真机证明 FRKTEST 的 23 行可被抓取；“一页 100 行”与超过一页的抓取能力
  均未证，因此删除了无代码语义的 `page_size_default` 配置。
- 只读流程失败不会产生重复业务写入，`safe_to_retry=True`。只有已经发出写请求且
  结果未知时才允许记录 `safe_to_retry=False`。
- `WindowOwnership.observe()` 已接入收尾：运行初始快照后新增的、可分类的 E10
  可见顶层窗口会记录精确 `(PID, HWND)` 并纳入清理；运行前已有窗口绝不关闭。
  无标题瞬态下拉浮层不属于该机制，由控件级流程自行关闭或随父窗销毁。
