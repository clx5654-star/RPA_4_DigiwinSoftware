# E10 RPA 本机任务控制面 v1 施工报告

日期：2026-09-22
范围：纯离线控制面开发；未提交或执行真实 E10 登录、查询、核对、写入任务。

## 验收结果

| 判据 | 实现位置 | 测试/证据 | 结果 |
|---|---|---|---|
| 严格任务协议、未知/危险字段拒绝 | `rpa_control/models.py` | `tests/test_control_models.py` | 通过 |
| FT 登录账套与 FRKTEST 业务参数分离 | `rpa_control/registry.py` | `test_login_and_business_environment_are_separate` | 通过 |
| 三个生产白名单工作流 | `rpa_control/registry.py` | `test_production_allowlist_has_only_three_reviewed_workflows` | 通过 |
| SQLite WAL、持久任务与事件 | `rpa_control/sqlite_queue.py` | `test_queued_job_survives_queue_reopen` | 通过 |
| 两 worker 原子领取 | `claim_next()` / `BEGIN IMMEDIATE` | 离线 summary 的 `successful_claims=1` | 通过 |
| fencing token 拒绝旧 worker | `_fenced_transition()` | `test_fencing_token_rejects_old_worker_update` | 通过 |
| 状态变更审计 | `job_events` | `test_job_event_history_records_transitions` | 通过 |
| Artifact 复制、扩展名及 SHA-256 | `rpa_control/artifacts.py` | `tests/test_control_artifacts.py` | 通过 |
| 篡改、UNC、`..`、路径重定向拒绝 | `rpa_control/artifacts.py` | artifact 4 项测试 | 通过 |
| 固定 argv、`shell=False` | `registry.py` / `executor.py` | `test_child_process_is_always_started_with_shell_false` | 通过 |
| exit code 0 不冒充成功 | `executor._classify_completed()` | `EXIT_ZERO_NO_TERMINAL → FAILED` | 通过 |
| 唯一 `run_finished` 和允许终态 | `scan_child_evidence()` | executor 离线测试 | 通过 |
| COMMIT intent/act 顺序与基数 | `_classify_completed()` | `test_commit_success_requires_intent_then_single_act` | 通过 |
| 登录提交后丢失不重试 | `executor.py` / `recover_expired_jobs()` | `LOGIN_SUBMIT_LOST → UNKNOWN` | 通过 |
| COMMIT 写动作后丢失不重试 | `executor.py` / `recover_expired_jobs()` | `COMMIT_ACT_LOST → REQUIRES_HUMAN` | 通过 |
| receiver 桌面不合规不领取 | `rpa_receiver._runtime_ready()` | `test_receiver_not_ready_does_not_claim_job` | 通过 |
| explain 不创建 DB、不启动 E10 | `rpa_receiver.py` | `test_receiver_explain_does_not_create_database_or_touch_e10` | 通过 |
| 任务/数据库不含凭据 | 协议禁字段、环境别名 | `test_submit_and_status_persist_without_credentials` | 通过 |
| 既有工程回归 | 全部 `tests/` | `python -m unittest discover -s tests -v` | 167 项完成：165 通过，2 项受限沙箱 DPAPI 测试跳过 |

DPAPI 两项集成测试已在真实 Windows 用户上下文单独运行，结果 3/3 通过；受限 Codex 沙箱没有
当前用户 DPAPI 主密钥，因此完整回归中按设计跳过其中两项。

## 离线闭环证据

命令：

```powershell
python tests\run_control_plane_acceptance.py
```

证据目录：

```text
runs/control_plane_v1_offline/20260922T085155Z/
```

汇总：`acceptance_summary.json`

- READONLY_SUCCESS → `SUCCEEDED`
- EXIT_ZERO_NO_TERMINAL → `FAILED`
- LOGIN_SUBMIT_LOST → `UNKNOWN`
- COMMIT_ACT_LOST → `REQUIRES_HUMAN`
- 两个并发 worker → 仅一个领取成功
- 关闭并重新打开 SQLite → 首个任务仍为 `SUCCEEDED`

每个场景的 evidence 目录包含 stdout、stderr、receiver JSONL、child JSONL、result 和无 artifact
声明。假工作流只位于 `tests/fixtures/`，生产白名单没有注册它，且验收记录明确
`e10_touched=false`。

## 路径与运行方式

- 触发器：`rpa_submit.py`
- 接收器：`rpa_receiver.py`
- 数据库：`state/rpa_jobs.sqlite3`
- 输入：`jobs/input/<artifact_id>/`
- 证据：`jobs/evidence/<job_id>/`
- 部署：任务计划程序“仅当用户登录时运行”，同一 E10/DPAPI Windows 用户，参数
  `rpa_receiver.py serve --attended --poll-seconds 2`

## 剩余限制

1. 按施工令已停止在离线验收；尚未通过队列执行真实 `e10.session.login` 或 verify。
2. E10 登录冷启动仍有已知暂态窗口过早判 UNKNOWN 的问题；直接位于登录页的登录段已真机通过。
3. 没有中央 HTTPS 服务、TLS 身份、多 VM 路由或 artifact 远程传输。
4. `dataset_epoch=FRKTEST-RESET-20260918` 固定在环境档案；测试数据库再次重置时必须评审更新。
5. UI 核对仍是 `external=false`，不构成生产账套的独立外部核对。
6. UNKNOWN/REQUIRES_HUMAN 没有自动重新排队，也尚无人工处置/解除界面。
7. 本机 SQLite 的 idempotency 唯一键偏保守；失败任务不会用同一键另建新任务，需要未来的显式
   人工处置协议，而不是删除记录或绕过防重。
