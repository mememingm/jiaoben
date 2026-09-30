# NexSim 批量工具交接文档

> 后续交接快照见 [current-handoff.md](../current-handoff.md)。本文件保留早期设计与演变记录，下文的 Git、服务状态和“当前版本”表述可能已过时。

> 更新日期：2026-09-29
> 最新界面以第 21 节为准；前文提交激活流程保留为历史说明。  
> 项目目录：`D:\esim\nexsim-batch`  
> 远端仓库：`https://github.com/mememingm/jiaoben.git`  
> 当前分支：`main`  
> 当前基线：`b62739e feat: separate platform activation from profile status`

## 1. 项目定位

本项目是一个本地 Web 工具，包含两套彼此独立的业务：

| 业务 | 入口 | 用途 | 不负责的事情 |
| --- | --- | --- | --- |
| eSIM Profile 状态查询 | `http://127.0.0.1:8765/` | 查询已开卡 eSIM 的 Profile 是否已经写入设备 | 不开户、不激活套餐、不获取二维码 |
| 平台开户注册 | `http://127.0.0.1:8765/activation.html` | 从平台可开户库存选卡、建立批次、准备二维码资料、提交平台开户并核验结果 | 不查询、也不改变 `esimProfileStatus` |

必须保持这两个概念分离：

- `esimProfileStatus` 描述 eSIM Profile 的下载/安装状态。
- 平台开户注册描述平台侧的库存、订单、号码和信号开通流程。
- 平台激活成功不等于 Profile 已经安装；Profile 是否变为 `INSTALLED`，仍需在状态查询页面单独确认。
- “按 ICCID 激活卡与状态查询”不是写入手机或 eSIM 设备，它只提交平台侧开户请求，并核对订单、号码和信号状态。

## 2. 当前交付状态

### 2.1 Git 状态

本地 `HEAD` 与 `origin/main` 当前都指向：

```text
b62739ee1b7fdd605b3c71ec671c3494348a33a8
```

但当前工作区有尚未提交、尚未推送的功能改动。远端仓库只包含上述基线，不能把远端当成当前完整版本。

当前已修改文件：

```text
README-eSIM状态查询工具.md
README.md
nexsim-status.example.json
nexsim_platform_activation.py
nexsim_platform_batch.py
nexsim_status_checker.py
nexsim_status_web.py
web/activation.html
web/activation.js
web/app.js
web/index.html
web/styles.css
```

本交接文档 `HANDOFF.md` 也是新增的本地文件。接手时先查看 `git status` 和 `git diff`，不要覆盖、回退或遗漏这些本地改动。

### 2.2 已确认与未确认

已确认完成：

- Profile 状态页和平台开户页已经分开。
- 服务曾在本机启动，`/api/health`、`/`、`/activation.html` 曾返回 HTTP 200。
- 平台页已有自动识别组织、套餐下拉、库存折叠、批次、二维码任务、预检、提交和核验界面。
- Profile 页已有进度、取消、筛选、导出、查询历史加载和删除。

尚未做真实业务验证：

- 未用真实平台账号对当前完整页面重新跑端到端流程。
- 未验证批量 200 张时的平台限流、耗时和平台任务稳定性。
- 未验证真实扣费、真实订单创建和异常恢复的全部分支。
- 本次生成交接文档没有运行测试、没有调用真实库存/二维码/激活接口。

## 3. 运行环境

### 3.1 依赖

- Python 3.10 或更高版本。
- 可访问 `https://admin.nexsimus.com` 的网络环境。
- Python 依赖：

```text
requests>=2.32.3,<3
qrcode[pil]>=8.0,<9
```

### 3.2 Windows 启动

推荐以前台方式运行，便于看到错误：

```powershell
Set-Location D:\esim\nexsim-batch
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 nexsim_status_web.py
```

统一入口也可以启动 Web 服务：

```powershell
.\.venv\Scripts\python.exe nexsim_status_tool.py --console
```

不加 `--console` 时，Windows 统一入口会尝试用 `pythonw.exe` 后台运行；没有控制台时，日志写入 `outputs\service.log`。

### 3.3 Linux / macOS 启动

```bash
cd /path/to/nexsim-batch
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python nexsim_status_web.py
```

### 3.4 启动参数

```text
--host             监听地址，默认 127.0.0.1
--port             监听端口，默认 8765
--output-dir       结果目录，默认项目下 outputs/
--profile-workers  Profile 并发数，默认 2，允许 1-16
```

示例：

```powershell
.\.venv\Scripts\python.exe -X utf8 nexsim_status_web.py --port 8766 --profile-workers 4
```

默认只监听本机。当前服务没有登录保护、TLS 或多用户权限系统；除非前面部署了带认证的反向代理，否则不要直接使用 `--host 0.0.0.0` 暴露到局域网或公网。

## 4. Profile 状态查询

### 4.1 操作流程

1. 打开 `http://127.0.0.1:8765/`。
2. 输入平台账号和密码。
3. 选择查询全部卡，或粘贴指定 ICCID。
4. 点击查询，等待进度完成；需要时可取消尚未完成的任务。
5. 使用“全部 / 已安装 / 已释放 / 查询失败”等筛选查看结果。
6. 下载 CSV，或在历史记录中重新加载、删除某次查询。

后端会固定库存筛选条件为：

```text
simType = ESIM
status  = USED
```

因此这里查询的是已经开卡的 eSIM，不会把 `ALLOCATED` 可开户库存混入结果。

### 4.2 状态解释

当前业务主要使用以下两种平台 Profile 状态：

| 状态 | 页面含义 |
| --- | --- |
| `INSTALLED` | Profile 已经写入/安装 |
| `RELEASED` | Profile 已释放、尚未写入 |

“查询失败”不是平台 `esimProfileStatus` 的第三个枚举值，而是本工具在登录、网络、限流或单卡接口异常时记录的查询结果。代码中通过 `esimProfileStatusQueryStatus = failed` 与 Profile 原始状态区分。

如果平台未来返回 `ERROR`、`UNAVAILABLE` 或其他未知值，前端会按异常/未知展示，不能擅自归类为 `INSTALLED` 或 `RELEASED`。

### 4.3 性能与重试

- 默认并发查询数是 2，可通过 `--profile-workers` 调整到 1-16。
- 并发过高可能触发平台限流，不应只为追求速度直接设置为 16。
- Profile 查询对明确的临时失败有有限重试和冷却等待。
- 查询结束时要看“结果是否完整”，不能只看进度达到 100%。存在失败记录时，不应据此完成全部卡的状态核对。

### 4.4 查询历史

每次查询会写入一组 JSON/CSV。页面支持：

- 查看全部查询记录。
- 加载任意一次历史结果。
- 删除指定历史记录；删除 JSON 时会同步尝试删除同名 CSV。

## 5. 平台开户注册

页面地址：`http://127.0.0.1:8765/activation.html`

### 5.1 完整流程

1. 输入后台账号和密码，点击“识别账号并加载套餐”。
2. 工具登录后自动读取当前账号的组织 ID，并加载可开户套餐。
3. 从下拉菜单选择套餐，点击“读取平台库存”。
4. 工具只把同时满足下列条件的库存标为可选：

```text
ownerOrgId = 当前账号自动识别的组织
productId  = 当前选择的套餐
inventoryType = P
simType = ESIM
status = ALLOCATED
qrCodeSupported = true
```

5. 设置目标数量，自动选择或人工勾选 1-200 张卡。库存表可以收起；收起不会清除当前选择。
6. 点击“建立平台开户批次”。这一步只在本地固定账号、组织、套餐和 ICCID 清单，不调用二维码或开户注册接口。
7. 对批次进行敏感操作前，人工核对批次摘要和下载的 ICCID CSV。
8. “一次性获取 LPA + 二维码”会调用有次数风险的接口。每张卡最多请求一次，结果未知时停止后续卡，不自动重试。
9. 点击“检查激活条件与价格”，执行只读预检；已删除手工填写金额上限。
10. 核对卡数、单价、总价、余额、重复订单和库存状态。
11. 确认实际总价后点击“按 ICCID 激活卡”。这是可能真实扣费、真实创建订单的写操作。
12. 提交结束后自动查询一次激活状态，页面逐张展示订单、号码、订阅与信号状态。也可点击“查询激活状态（只读）”，查询不依赖本地二维码文件或本工具提交记录，支持手工激活后的核对。

### 5.2 “按 ICCID 激活卡与状态查询”的作用

这个区域处理平台侧业务闭环：

- “只读预检”重新读取实时库存、产品、价格、余额和已有订单，不产生订单。
- “明确提交”把批次 ICCID 作为一次性开户请求提交给平台，可能扣费并创建订单/号码。
- “结果核验”只读查询平台订单和 subscriber 信息，确认订单、号码、订阅状态和信号状态是否全部满足成功条件。

它不会把二维码写入设备，也不会把 Profile 状态直接改为 `INSTALLED`。人工或设备完成 eSIM 写入后，仍需回到 Profile 状态页另行查询。

## 6. 操作风险分级

| 操作 | 类型 | 是否访问平台 | 主要风险 |
| --- | --- | --- | --- |
| 识别账号和加载套餐 | 只读 | 是 | 登录失败或账号组织识别错误 |
| 读取库存 | 只读 | 是 | 平台限流、读取到旧状态 |
| 建立 `pb-*` 批次 | 本地写入 | 否 | 选错账号、套餐或 ICCID |
| 加载已有批次 | 本地读取 | 否 | 旧批次归属信息不足，只能只读 |
| 获取 LPA/二维码 | 敏感、一次性 | 是 | 可能消耗查看次数；超时后结果未知 |
| 下载已保存二维码 | 本地读取 | 否 | 敏感资料泄露 |
| 开户预检 | 只读 | 是 | 预检后到提交前平台状态仍可能变化 |
| 提交开户注册 | 真实写操作 | 是 | 可能扣费、创建订单；未知结果不能重发 |
| 结果核验 | 只读 | 是 | 平台异步处理时可能暂未确认 |

账号密码只应在当前任务内存中存在，不应写入浏览器存储、批次、日志或 Git。二维码、LPA、ICCID、订单回包同样属于敏感数据。

## 7. 一次性操作与禁止重试规则

### 7.1 二维码任务

批次状态大致为：

```text
inventory_selected
  -> preparing_qr
  -> qr_ready | qr_partial | qr_failed
```

关键保护：

- 批次第一次触发后会记录 `qr_operations` 和逐卡 attempt marker。
- 同一批次不能再次整体触发二维码获取。
- 请求成功后二维码 PNG 会先落盘，再在任务结果中返回 LPA；批次 JSON 不保存 LPA 原文。
- 网络超时、连接中断或无法判断平台是否已扣次数时，状态记为 `unknown`，立即停止后续卡。
- `unknown` 不是“失败后可以再试”，而是“平台可能已经处理，禁止再次调用”。
- 如果本地保存的二维码丢失，不要通过重复请求来补救；先备份 `outputs/`，再由平台人工确认是否存在安全恢复方式。

### 7.2 开户提交

开户工件状态大致为：

```text
not_started
  -> submission_attempted
  -> submitted | unknown
  -> verified
```

提交前会以独占方式先写入 `intent.json`，再调用平台接口。只要 `intent.json` 已存在，就代表这个批次已经尝试提交，不能再次提交。

如果出现以下任一情况：

- 页面超时或断开；
- 服务进程退出；
- 平台返回格式异常；
- 已有 `intent.json`，但没有明确成功结果；

都不得点击或调用提交接口重发。正确处理方式是重新登录同一账号、加载同一批次，然后执行“核验激活结果”，必要时到平台后台人工核对订单。

## 8. 账号上下文与批次隔离

平台开户页的账号上下文由以下三项组成：

```text
base_url + org_id + username
```

- `org_id` 从登录结果自动识别，不再要求人工填写。
- 新批次会保存上述账号标识，但不会保存密码或 token。
- 页面只列出与当前账号上下文匹配的批次。
- 对已有批次执行二维码、预检、提交或核验时，后端会重新登录并校验组织。
- 服务重启后，内存中的账号上下文会消失；必须重新输入账号密码并执行“识别账号并加载套餐”。
- 没有 `owner` 信息的旧批次只能单独标注并按兼容规则只读展示，不能自动视为当前账号可操作批次。

## 9. 数据目录与备份

默认数据目录是：

```text
D:\esim\nexsim-batch\outputs\
```

主要内容：

```text
outputs/
├─ service.log
├─ esim-status-raw-*.json
├─ esim-status-raw-*.csv
├─ platform-activation-batches/
│  ├─ pb-*.json
│  ├─ pb-*.csv
│  └─ <batch-id>/
│     ├─ qr/
│     │  └─ <sequence>-<iccid>.png
│     ├─ qr-attempts/
│     │  └─ *.json
│     └─ platform-activation/
│        ├─ activation.csv
│        ├─ intent.json
│        ├─ response.json
│        ├─ unknown.json
│        ├─ job-status.json
│        ├─ verification.json
│        └─ verification.csv
└─ write-batches/
   └─ wb-*  （旧版兼容记录）
```

说明：

- `esim-status-raw-*`：Profile 查询历史。
- `pb-*.json`：批次元数据、归属、逐卡二维码处理状态。
- `pb-*.csv`：批次 ICCID 清单。
- `qr/`：已保存的敏感二维码图片。
- `qr-attempts/`：一次性请求前后状态，用于阻止危险重试。
- `platform-activation/intent.json`：提交意图和幂等保护依据。
- `response.json`、`unknown.json`、`job-status.json`：提交回包或未知状态证据。
- `verification.csv`：允许从页面下载的核验结果。

`outputs/` 已被 `.gitignore` 排除，但它是业务恢复所需的关键目录。升级、迁移或重装前必须整体备份，并按敏感数据管理。不要把它压缩后上传到公开仓库。

## 10. 核心代码

| 文件 | 责任 |
| --- | --- |
| `nexsim_status_web.py` | HTTP 服务、任务管理、静态页面和 API 路由 |
| `nexsim_status_checker.py` | Profile 库存读取、状态查询、重试和结果保存 |
| `nexsim_platform_batch.py` | 平台库存资格判断、`pb-*` 批次、二维码一次性任务 |
| `nexsim_platform_activation.py` | 账号组织/套餐读取、预检、提交、轮询和核验 |
| `nexsim_status_tool.py` | 统一启动入口 |
| `web/index.html` | Profile 状态页面 |
| `web/app.js` | Profile 页面交互与状态展示 |
| `web/activation.html` | 平台开户注册页面 |
| `web/activation.js` | 平台账号、库存、批次、二维码、提交和核验交互 |
| `web/styles.css` | 两个页面的公共样式 |

服务实际加载的是 `web/` 目录下的前端文件。项目根目录也保留了部分同名 HTML/JS/CSS 文件，但当前 `nexsim_status_web.py` 不会提供它们；修改页面时应以 `web/` 为准，避免改错副本。

## 11. HTTP API 概览

### 11.1 健康检查

```text
GET /api/health
```

### 11.2 Profile 查询

```text
POST /api/jobs
GET  /api/jobs/<job-id>
POST /api/jobs/<job-id>/cancel
GET  /api/history
GET  /api/history/latest
GET  /api/history/<filename>
DELETE /api/history/<filename>
```

### 11.3 平台账号、库存和批次

```text
POST /api/platform-context-jobs
GET  /api/platform-context-jobs/<job-id>
POST /api/platform-inventory-jobs
GET  /api/platform-inventory-jobs/<job-id>
GET  /api/platform-batches
POST /api/platform-batches
GET  /api/platform-batches/<batch-id>
GET  /api/platform-batches/<batch-id>/export.csv
```

### 11.4 二维码一次性任务

```text
POST /api/platform-qr-jobs
GET  /api/platform-qr-jobs/<job-id>
POST /api/platform-qr-jobs/<job-id>/cancel
GET  /api/platform-batches/<batch-id>/qr/<sequence>
```

二维码任务接口不能用于探测或试跑；调用就是敏感业务动作。

### 11.5 开户和核验

```text
POST /api/platform-activation/preview
POST /api/platform-activation/submit
POST /api/platform-activation/verify
GET  /api/platform-activation-jobs/<job-id>
GET  /api/platform-batches/<batch-id>/platform-activation/verification.csv
```

`preview` 和 `verify` 是只读平台操作；`submit` 是真实写操作。

## 12. 已知限制

- 当前是单机本地工具，不是带权限系统的多租户服务。
- 任务状态主要保存在服务进程内存中；服务重启后正在运行的任务不会恢复，但已经落盘的批次和工件仍在。
- 平台批次目前没有网页删除功能，避免误删一次性操作证据；如确需清理，应先完整备份并人工审核目录。
- Profile 历史支持删除，删除后无法从工具恢复。
- LPA 原文只在当前二维码任务结果内存中出现，不写入批次文件；二维码 PNG 会写入磁盘。
- 二维码图片是敏感且不可安全重建的数据，本地磁盘丢失可能导致实际损失。
- 预检与提交之间存在时间窗口，平台库存、价格和余额仍可能变化；提交函数会再次执行预检，但不能消除所有平台侧竞态。
- 当前平台接口适配来自既有脚本和已观察的接口行为，不等同于官方稳定 SDK；平台字段或路由变化时应停止写操作，先用只读请求核对。
- 没有完整自动化测试套件和真实沙箱环境，尤其不能用生产二维码或真实扣费流程当普通测试。

## 13. 故障处理

### 服务无法启动

1. 确认 Python 版本和依赖已安装。
2. 检查 8765 端口是否被占用；必要时换端口。
3. 前台启动查看报错；后台启动则查看 `outputs/service.log`。
4. 不要删除 `outputs/` 来“重置”服务。

### 重启后看不到批次

1. 重新输入原账号密码。
2. 点击“识别账号并加载套餐”。
3. 确认识别到的组织与原批次一致。
4. 点击刷新批次。

### Profile 查询结果不完整

1. 保留本次 JSON/CSV，不要把失败记录解释为 `RELEASED`。
2. 查看失败原因和成功数量。
3. 确认网络、账号和平台限流后，再发起新的只读查询。

### 二维码任务中断或结果未知

1. 不要刷新后再次点击“一次性获取”。
2. 备份该批次 JSON、`qr-attempts/` 和 `qr/`。
3. 查看批次中每张卡的 `qr_status` 和 attempt marker。
4. 让平台方确认查看次数和卡片状态；没有明确证据前禁止重试。

### 开户提交中断或结果未知

1. 检查是否已有 `platform-activation/intent.json`。
2. 只要 intent 存在，就禁止再次提交。
3. 重新登录同一账号并执行结果核验。
4. 同时到平台后台按 ICCID、组织和套餐核对订单。
5. 保留 `response.json`、`unknown.json` 和 `job-status.json` 作为证据。

## 14. Git 与敏感文件规则

提交前必须排除：

```text
config.json
outputs/
真实账号或密码文件
二维码图片
LPA / activationCode
真实 ICCID 批次导出
平台原始回包和订单数据
```

建议接手顺序：

```powershell
Set-Location D:\esim\nexsim-batch
git status --short
git diff --stat
git diff -- README.md nexsim_status_web.py nexsim_platform_batch.py nexsim_platform_activation.py web
```

确认内容后只按明确文件名暂存，不使用会把未知敏感文件一并加入的宽泛命令。提交和推送前再次检查 `git diff --cached`。

## 15. 接手检查清单

- [ ] 阅读本文件与 `README.md`。
- [ ] 确认当前目录、远端、分支和 commit。
- [ ] 查看并保存当前未提交 diff，不覆盖现有改动。
- [ ] 确认 `config.json` 和 `outputs/` 仍被忽略。
- [ ] 使用前台方式启动服务，先只检查健康接口和静态页面。
- [ ] 不使用真实账号做普通测试。
- [ ] Profile 查询与平台开户注册分开验收。
- [ ] 任何二维码操作前确认批次、备份目录和一次性风险。
- [ ] 任何开户提交前核对组织、套餐、数量、实际总价和余额。
- [ ] 二维码结果未知时不重试；开户结果未知时不重发，只核验。
- [ ] 提交代码前复查是否混入账号、二维码、LPA、ICCID 或平台回包。

## 16. 后续建议

以下是建议，当前未执行：

1. 先对本地未提交改动做人工代码审查，再决定如何拆分 commit 和推送。
2. 增加“只读演练模式”，用脱敏 fixture 验证页面和状态机，避免拿真实二维码或真实扣费接口做测试。
3. 为生产使用增加本地访问口令或反向代理认证，并限制 `outputs/` 的文件权限。
4. 增加受控备份功能，只备份业务恢复所需文件，并对二维码目录加密。
5. 与平台确认正式 API 字段、限流、幂等和二维码恢复政策后，再扩大到 200 张批量操作。


## 17. 2026-09-29 激活状态界面修订

- 删除金额上限输入和对应后端上限校验，保留实时价格、余额校验及实际总价确认；价格变化时阻止旧确认提交。
- 核验不再要求本工具的提交记录或本地二维码文件，只使用批次身份及订单、号码状态。
- 正常提交返回后自动核验一次；查询失败或条件不齐时只显示未确认，允许稍后只读查询，不重发激活。
- 页面逐卡显示激活结果；新查询保存 verification-result.json 以供重新加载。旧 CSV 的存在不再代表全部激活成功。
- 本轮只进行本地模拟验证，没有调用真实二维码、激活或扣费接口。

## 18. 2026-09-29 简化为激活状态检查（当前界面）

- 区域改为“检查是否已激活”，移除页面预检、价格确认与提交激活按钮及其前端调用。
- 用户完成 LPA / 二维码获取后，前端自动调用一次只读 verify；保留“重新检查”和结果导出。
- 页面仅显示 ICCID、状态和说明。已满足成功条件显示“已激活”，证据不完整显示“未确认”。
- 后端既有提交接口保留兼容，当前页面不再调用；自动检查绝不提交激活。
- 按用户要求，本轮未运行测试、浏览器检查或真实平台请求。

## 19. 2026-09-29 补回 ICCID 激活闭环（当前界面）

- 上一轮只查询激活状态，未提交激活，不符合用户实际流程。
- 获取资料成功后自动准备激活，弹窗展示批次卡数及实际总价；用户确认后提交一次激活，后端随后核验。
- 已有资料的批次提供“激活并检查”按钮，不重复获取二维码。部分资料获取成功的批次仍只检查，完整准备后才可提交。
- 保留“重新检查”；已提交或响应未知的批次不能再次提交，保留服务端 intent 保护。
- 本次仅修改前端和文档，未操作真实卡片，未运行测试。

## 20. 2026-09-29 恢复授权金额上限（当前版本）

- 页面恢复“本次授权金额上限”，预检及提交同时传 max_total；提交仍传 confirmed_total 绑定确认时的总价。
- 后端预检和提交前重检均校验金额上限，超限时不提交；仅查询状态不要求上限。
- 兼容旧服务的 max_total 参数要求；旧服务提交后没有附带核验时，页面继续发起只读核验。
- 后端源码修改需重启服务加载。本轮按要求未运行测试或提交真实激活。

## 21. 2026-09-29 开户套餐对齐（当前版本）

- 已从平台公开 ActivationCenterView-BeHu0jFW.js 确认 P001—P004 / A001 的映射和筛选条件，详见 docs/activation-products.md。
- 接口结果额外按 cardCategory、productPurpose=BASE_PLAN、activationEnabled=1 筛选，排除非开户套餐。
- 下拉和选中详情显示平台编号、名称、packageSpec 和单价；激活确认包含所选套餐，提交仍用原始 productId。
- A 卡基础套餐禁用，后端也阻止 A 卡库存读取或开户；历史批次不切换产品，只读查询仍可使用。
- 新前端要求 product_catalog_version=1，需要重启服务后重新识别账号。
- 本轮未运行测试、未读取用户卡片资料、未调用真实业务接口。
