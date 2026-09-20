# Agent Server Ops

让 AI Agent 通过一个独立网关管理服务器：持久任务、可追踪日志、文件传输、可配置部署流程，以及开箱可安装的 `server-ops` skill。

A standalone operations gateway, JSON CLI, and portable Agent skill. No application framework, database service, cloud account, or model API is required.

```text
Agent + server-ops skill
        │  JSON CLI · named server profiles · private credentials
        ▼
 HTTPS / private network / SSH tunnel
        ▼
 Independent gateway ── SQLite receipts + NDJSON logs
        │
        ├── durable shell commands and process cancellation
        ├── administrator-defined deployment/service stages
        └── workspace file transfer with SHA256 checks
```

网关安装在业务项目之外。业务应用停止、升级失败、客户端断线时，管理入口和任务记录仍独立存在。

## 能做什么

| 能力 | 行为 |
| --- | --- |
| 多服务器 | 按学校、项目、节点或兼容别名选择；目标不唯一时拒绝操作，回执绑定目标配置身份 |
| 远程执行 | Linux/macOS 用 `sh`，Windows 用 PowerShell；支持工作目录、超时、取消 |
| 持久任务 | 接收后存 SQLite，客户端断线继续执行；状态、阶段、退出码均可查询 |
| 请求去重 | 客户端先写回执，再发请求；同一请求键返回同一个任务 |
| 并发与互斥 | 按任务类型限流，相同资源串行；每个状态目录只允许一个调度器 |
| 重启恢复 | 保留排队任务；运行任务标记中断，核对 PID 创建时间后清理已知遗留进程 |
| 文件传输 | 限定 workspace，校验路径和 SHA256，原子落盘，默认不覆盖 |
| 通用操作 | 把部署、重启服务、备份或检查配置成有名称的多阶段操作 |
| Agent skill | 包内携带，`server-ops install-skill` 安装；也可复制给其他兼容 Agent |

这是 **v0.1**。它提供管理基础设施，不包含特定项目逻辑、MDM、云厂商控制台、反向代理、交互 PTY 或 Web 管理界面。所有操作以 CLI/API 完成。任务执行权限等于网关运行账户；Linux 的 sudo 安装默认使用 root，Windows 开机任务使用 SYSTEM，macOS 使用当前登录用户。

## 1. 安装到服务器

需要 **Python 3.11+、pip、venv 和 Git**。部署目标可以是 Linux、Windows 或 macOS；容器里安装只管理容器自身，管理宿主机请原生安装。

```bash
git clone --branch v0.1.0 https://github.com/quasar2333/agent-server-ops.git
cd agent-server-ops
```

### Linux：systemd 开机常驻

```bash
sudo python3 install/install.py --root /opt/agent-server-ops --service
sudo systemctl status agent-server-ops
curl --fail http://127.0.0.1:9876/ops/healthz
```

安装器创建独立虚拟环境、随机 token、配置、workspace 和状态目录，注册 systemd 并启动。不会修改已有应用或开放防火墙。缺少 venv 的发行版须先安装相应 Python venv 包。要用普通账户运行，可去掉 `--service`，由该账户前台启动或自行配置进程管理器。

### Windows：开机任务，无需登录

在**管理员 PowerShell** 中：

```powershell
py -3 install/install.py --root C:\ProgramData\AgentServerOps --service
Get-ScheduledTask -TaskName AgentServerOps
Invoke-RestMethod http://127.0.0.1:9876/ops/healthz
```

使用 SYSTEM 开机任务，失败后重启；安装目录 ACL 限制为安装账户、SYSTEM 和管理员。运行前确认 `py -3 --version` 至少为 3.11。

### macOS：当前用户 LaunchAgent

```bash
python3 install/install.py --root "$HOME/Library/Application Support/AgentServerOps" --service
curl --fail http://127.0.0.1:9876/ops/healthz
```

不使用 sudo。LaunchAgent 随该用户登录启动，不提供无人登录的系统级服务。

### 任意平台：前台运行 / 本地试用

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/server-ops-gateway init --root "$HOME/.local/share/agent-server-ops-demo"
.venv/bin/server-ops-gateway serve --config "$HOME/.local/share/agent-server-ops-demo/gateway.json"
```

Windows 对应 `.venv\Scripts\python.exe` 和 `.venv\Scripts\server-ops-gateway.exe`，将路径替换为自己的目录。

### 连接方式

默认只监听 `127.0.0.1:9876`。可以选一种：

- **SSH 隧道**：在 Agent 机器运行 `ssh -L 9876:127.0.0.1:9876 user@server`，客户端配置 `http://127.0.0.1:9876`。服务器装好后，执行操作通过网关进行。
- **内网/VPN**：安装时加 `--bind <服务器内网IP>`；客户端登记时显式加 `--allow-http`。仅用于受信任网络。
- **HTTPS**：通过现有 Caddy/Nginx 把独立域名代理到网关，或前台命令加 `--certfile cert.pem --keyfile key.pem`。原生常驻服务使用直接 TLS 时，把这两个参数加入生成的启动配置并重启。自有 CA 使用客户端 `--ca-file`。

网关不绑定业务域名，不接管业务端口。公开网络应使用 HTTPS，访问范围由防火墙/VPN 决定。token 代表该网关账户的操作权限，请按管理凭据保存；初始化只打印凭据文件路径。

没有现有 HTTPS 证书时，可在安装目录虚拟环境安装可选依赖 `pip install '.[tls-setup]'`，从独立管理员会话运行 `install/prepare_tls.py --root <安装目录> --hostname <域名> --recipient <接收方X25519公钥的64位十六进制>`。它生成一年有效的独立自签证书和权限受限的私钥，输出用接收方公钥加密的 token 回执；不会修改服务、开放端口、覆盖已有 TLS 目录或更换 token。Windows 安装根目录须先由安装器设置管理员/SYSTEM ACL。

接收方私钥只保留在运维机器的私有文件，不能进入服务器命令、日志或 Git。通过已核实的管理员通道记录回执中的证书 SHA256 指纹，解密 token 后只写入本机私有凭据文件。先核对实际 HTTPS 证书指纹，再把该证书作为该配置的 `--ca-file`；不要为方便接入关闭证书校验。证书的续期、服务参数切换和公网映射仍需单独操作和验收；该脚本不提供自动续期或服务安装。

## 2. 安装 Agent 客户端和 skill

在运行 Agent 的机器上，使用其可调用的 Python 环境：

```bash
python -m pip install "git+https://github.com/quasar2333/agent-server-ops.git@v0.1.0"
server-ops install-skill
```

skill 默认安装到 `${CODEX_HOME:-~/.codex}/skills/server-ops`，也可用 `--target /path/to/skills/server-ops`。已有目录会保留并报错，不静默覆盖。其他 Agent 可使用仓库内的 [SKILL.md](agent_server_ops/skills/server-ops/SKILL.md)，并安装同一个 Python 包。

将服务器安装目录下的 `gateway.token` 安全复制到 Agent 机器的私有文件，例如 `~/.config/server-ops/production.token`。不要把内容粘贴到对话、仓库或命令参数中。POSIX 上设置 `chmod 600`；Windows 上保存在当前用户私有目录。

```bash
server-ops server add production --url https://ops.example.com --token-file ~/.config/server-ops/production.token
server-ops server list
server-ops --server production health
server-ops --server production status
server-ops --server production operation run python-check --stream
```

也支持 `--token-env SERVER_OPS_TOKEN`，由 Agent 运行环境提供变量值。全局 `--server`、`--config` 放在子命令之前。配置文件只记录凭据来源，不保存 token 内容。

### 按学校和项目管理多台服务器

每台机器分别安装独立网关，使用各自的凭据、状态目录和启动服务。客户端统一登记，默认名称为 `学校/项目/节点`；节点可用“生产”“影子验证”“存储”等角色，并在同一学校和项目内保持唯一。示例：

```bash
server-ops server add --school 东城区培新小学 --project 电子书包-整书阅读 --node 影子验证 --url https://shadow-ops.example.com --token-file ~/.config/server-ops/shadow.token
server-ops --school 东城区培新小学 --project 电子书包-整书阅读 --node 影子验证 status
server-ops --server 东城区培新小学/电子书包-整书阅读/影子验证 operation run python-check
```

已核对身份的旧配置可以补标签，原别名、URL、凭据引用和未知配置字段保持不变：

```bash
server-ops server label production --school 东城区培新小学 --project 电子书包-整书阅读 --node 生产
```

`server add/label` 后面的标签用于登记；命令前的全局标签用于筛选。标签筛选必须只命中一台，不能与 `--server` 混用。**多服务器配置不再隐式使用 default**，旧脚本应明确添加 `--server default`；仅剩一台且别名为 default 时保留原行为。没有批量执行或自动分流，避免一次操作落到多台机器。

每次操作的 JSON 结果和新提交回执都附带 `target`（别名、学校、项目、节点、URL、`profileId`），不输出凭据引用或 token。配置标签可修改，`profileId` 保持稳定；回执核对 URL 和配置身份，跨配置恢复会被拒绝。旧回执按 URL 和原别名检查。配置写入使用互斥锁和原子替换，遇到残留 `.lock` 时需先核对是否仍有编辑进程。

标签及 `profileId` 是客户端防误选机制，不是服务器的加密身份证明。首次登记仍须核对主机名、独立凭据和 TLS 证书；同一 URL 被重新指向另一台机器时，不能仅凭旧标签认定是原主机。此客户端兼容 Agent Server Ops 网关；其他项目的专用网关需通过其原生客户端操作，不能仅登记 URL 就假定协议兼容。

随后可以直接对 Agent 说：

> 用 server-ops 检查 production 的磁盘、内存和最近失败的任务。
>
> 用 server-ops 在 production 执行 deploy-web，跟踪到完成并验证应用健康。
>
> 把这份日志从 production 下载到本地，分析服务启动失败的原因。

## 3. 执行、日志和恢复

```bash
server-ops --server production run --command "echo ready" --stream
server-ops --server production run --command-file ./maintenance.sh --resource web --detach
server-ops --server production job list
server-ops --server production job status JOB_ID
server-ops --server production job logs JOB_ID --cursor 0
server-ops --server production job wait JOB_ID --wait-seconds 60 --stream
server-ops --server production job cancel JOB_ID
```

`run` 和 `operation run` 默认等待 60 秒。超出等待窗口返回退出码 **2**，任务继续；用 `job wait` 接着查询。`--detach` 立即返回接收回执。JSON 输出在 stdout，流式日志和回执路径在 stderr。终态成功是 `status=succeeded`、`ok=true`、`exitCode=0`；失败退出码 **1**。

提交前，客户端会保存一个权限受限的 JSON 回执，包含服务器 URL、请求键和完整请求体。可用 `--receipt /private/path/request.json` 指定文件，默认位于客户端配置目录的 `receipts/`。若提交结果因断线未知：

```bash
server-ops --server production job reconcile /private/path/request.json
```

同一个请求键、相同任务定义只接收一次；内容不同返回 409。回执不会包含 token，但可能包含命令里的敏感内容。

`job resume JOB_ID` 是一次**从头执行全部阶段的新任务**，只允许最初声明 `retrySafe=true` 的失败任务。普通命令可在确实安全时加 `--retry-safe`。网关重启不会自动重放运行任务；重试前需判断已产生的副作用。请求去重不承诺任意外部副作用的 exactly-once。

任务默认锁 `host`。自定义操作可锁 `web` 等应用名；手动修改同一应用也必须传 `--resource web`。锁只约束同一个网关内声明相同资源的任务，不覆盖 SSH 或其他本机进程。文件传输是独立接口，不自动取得任务资源锁。

## 4. 配置项目自己的部署流程

编辑服务器私有的 `gateway.json` 中的 `operations`，无需修改网关代码。每个操作可以配置：

| 字段 | 含义 |
| --- | --- |
| `description` | 给 Agent 看的用途 |
| `kind` | query / command / build / deploy / service / transfer |
| `cwd` | 已存在的绝对工作目录；默认 workspace |
| `timeout` | 所有阶段总超时，1–14400 秒 |
| `resources` | 非空互斥资源列表；默认 `["host"]` |
| `retrySafe` | 是否允许失败后显式从头重试；默认 false |
| `stages` | 有序的 `{name, command}`；command 可为 shell 字符串或 argv 数组 |

可从 [Docker Compose 操作示例](examples/compose-operations.json) 开始，把该 JSON 对象的条目合并到 `operations`。先调整 `/srv/web`、镜像和健康检查 URL。示例包含配置校验、拉取镜像、启动和健康检查；应用应使用固定版本或镜像 digest。服务器需自行安装 Docker Compose 和 curl，并让网关账户有对应权限。可同样配置 Git 发布、systemctl、PowerShell、备份程序或自己的部署脚本。

```bash
/opt/agent-server-ops/.venv/bin/server-ops-gateway check-config --config /opt/agent-server-ops/gateway.json
sudo systemctl restart agent-server-ops
server-ops --server production operation list
server-ops --server production operation run deploy-web --stream
```

每个阶段退出非零即停止后续阶段。部署是否回滚、怎样验证版本、怎样保留构建产物由具体项目的脚本定义；失败与取消本身不回滚。建议把关键命令分成独立阶段，或在脚本中显式检查每个退出码。POSIX shell 开启 `set -eu`，但不为管道提供 `pipefail`；PowerShell 的原生命令也须逐条检查退出码。

可把 `allow_shell` 设为 `false`，关闭通用远程 shell，仅使用命名操作和文件接口。这不是多租户沙箱或细粒度 RBAC：文件写入仍可影响读取该文件的操作。只向受信任的管理者提供 token。

## 5. 文件传输

```bash
server-ops --server production files list
server-ops --server production files upload ./release.zip releases/release.zip
server-ops --server production files download logs/result.txt ./result.txt
```

远端路径相对 `workspace`，拒绝路径越界和指向外部的符号链接。上传默认最多 64 MiB，可通过 `max_upload_bytes` 调整到最多 1 GiB；流式传输并验证 SHA256 后原子落盘。目标存在时需要 `--overwrite`。上传断线不会替换目标；若响应丢失，下载目标核对内容后再决定是否重传。下载采用临时文件后原子保存，回执包含本地 SHA256。

## 6. 升级、凭据和维护

安装器只做新安装，已有配置或 token 时退出保留现场。网关自身升级应从独立管理员会话执行：

1. 查询/处理当前任务，停止原生服务；停止时运行任务会中断，排队任务保留。
2. 备份安装目录中的 `gateway.json`、`gateway.token` 和停止后的 `state/`。
3. 在安装目录的虚拟环境中运行 `python -m pip install --upgrade "git+https://github.com/quasar2333/agent-server-ops.git@<已验证版本>"`。
4. 运行 `check-config`，启动服务，检查 `/ops/healthz`、`status` 和一个命名检查操作。

不要在网关自己的任务里替换/停止网关。保留旧版本号，可停止服务后安装原版本恢复。v0.1 没有后台自动升级或无人值守 schema 迁移。

Linux 使用 `systemctl stop/start/restart agent-server-ops`，日志见 `journalctl -u agent-server-ops`。Windows 使用 `Stop-ScheduledTask` / `Start-ScheduledTask -TaskName AgentServerOps`。macOS 使用 `launchctl bootout/bootstrap gui/$(id -u) <plist绝对路径>`，日志在安装目录。取消注册服务时保留配置、token、workspace 和 state，除非明确要删除数据。

token 是 `secrets.token_urlsafe(48)` 生成的随机值，网关配置中只保存其 SHA256。轮换时从服务器管理员会话生成新 token，更新 `gateway.token` 及 `gateway.json.token_sha256`，安全更新客户端凭据文件，再重启网关；旧 token 随即失效。轮换不会清空任务数据。

每个任务日志上限约 16 MiB，超出后记录截断标记并继续排空输出；任务数量和历史 SQLite 记录暂不自动清理。监控状态盘空间，需归档时先停止网关并整体备份状态目录。不要在活动数据库里手工删除行。网关没有连接保活中继，NAT/防火墙后的主机需要可达的 HTTPS、VPN 或隧道。

## API

除健康检查外均需 `Authorization: Bearer <token>`。任务提交、命名操作、resume 需 `Idempotency-Key`（8–128 字符）。无 cookie、无 query token，客户端拒绝跳转以避免转发凭据；不启用跨域浏览器调用。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /ops/healthz` | 公共、最小网关健康 |
| `GET /ops/api/status` | 主机指标 |
| `GET /ops/api/operations` | 操作名称、说明和阶段 |
| `POST /ops/api/operations/{name}` | 提交服务器配置的操作，body 为 `{}` |
| `POST /ops/api/jobs` | `{command, cwd?, timeout?, resources?, retrySafe?}` |
| `GET /ops/api/jobs?limit=50&before=...` | 时间游标分页 |
| `GET /ops/api/jobs/{id}` | 任务回执 |
| `GET /ops/api/jobs/{id}/logs?cursor=0` | NDJSON 解析结果、字节游标 `nextCursor` |
| `POST /ops/api/jobs/{id}/cancel` | 取消排队或运行任务 |
| `POST /ops/api/jobs/{id}/resume` | 显式重试允许重试的失败任务 |
| `GET /ops/api/files?path=` | 列目录 |
| `GET /ops/api/files/content?path=...` | 下载 |
| `PUT /ops/api/files/content?path=...&overwrite=false` | 上传；需 `X-Content-SHA256` |

同一状态目录禁止多个进程/worker；不同目录之间也不共享资源锁。这里采用单 Uvicorn worker，启动器和文件锁共同执行这一要求。进程管理方式参见 [Uvicorn 官方部署说明](https://www.uvicorn.org/deployment/) 和 [systemd 官方服务配置](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。

## 开发与验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m build
```

测试覆盖鉴权、幂等冲突、资源互斥、日志游标、失败与重试、取消、超时、启动恢复、路径限制、上传校验，以及真实 HTTP 网关上的 CLI 往返。强杀进程恢复测试在 POSIX 执行。GitHub Actions 对 Linux、Windows、macOS 运行测试和打包；原生服务注册仍需目标系统权限，自动化测试不修改 CI 宿主机的开机配置。

通用持久任务队列源于作者维护的 Bubu 运维工具，并移除了业务依赖。MIT License。
