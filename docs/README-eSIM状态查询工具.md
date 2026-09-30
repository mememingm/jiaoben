# eSIM Profile 状态查询工具

这个工具现在采用“HTML 页面 + Python 后台服务”的结构。浏览器负责输入和展示，Python 服务负责登录、并发查询、进度和导出，因此同一套工具可以运行在 Windows、Linux、macOS 或一台局域网服务器上。

```text
浏览器 HTML
    ↓ HTTP
Python Web 服务
    ↓ 只读请求
NexSim 管理后台
```

## 启动 Web 服务

Windows PowerShell：

```powershell
Set-Location D:\esim\nexsim-batch
.\.venv\Scripts\python.exe nexsim_status_tool.py
```

Windows 下默认会自动转为后台无窗口运行，浏览器页面仍然使用 `http://127.0.0.1:8765`。如果需要查看服务日志或排查启动问题，可以显式使用：

```powershell
.\.venv\Scripts\python.exe nexsim_status_tool.py --console
```

无窗口模式的服务日志保存在 `outputs/service.log`；正常使用时不需要打开它。

Linux / macOS：

```bash
cd /path/to/nexsim-batch
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 nexsim_status_tool.py
```

启动后打开：<http://127.0.0.1:8765>

- Profile 状态查询：<http://127.0.0.1:8765/>
- 平台批量开户激活：<http://127.0.0.1:8765/activation.html>

页面中输入后台账号和密码，点击“获取 eSIM 状态”。组织 ID 会从登录结果自动识别。

服务默认只监听本机。如果要让同一局域网的其他设备访问：

```bash
python3 nexsim_status_tool.py web --host 0.0.0.0 --port 8765
```

局域网或公网部署时，应在反向代理后增加 HTTPS、访问认证和防火墙规则。当前内置服务适合本机或受控内网使用，不是完整的公网身份认证系统。

## 查询和筛选

- Profile 查询固定只处理平台库存状态为 `USED` 的已成功开卡 eSIM，也可在该范围内限定指定 ICCID；
- 查询过程中显示整体进度、当前阶段和百分比；
- 结果可按 ICCID 或原始字段搜索；
- 可筛选“已写入（INSTALLED）”；
- 可筛选“未写入（RELEASED）”；
- 可筛选 Profile 异常原始值；
- 可筛选 Profile 查询失败；
- 可将当前筛选列表中的 ICCID 一键导出为 TXT，每行一个 ICCID；
- 单击记录查看完整原始字段；
- 结果自动保存为 JSON 和 CSV。

## 写卡批次准备（当前不获取二维码）

页面里的“准备写卡批次”只做写卡前的清单整理，不会获取二维码、读取 `activationCode`/LPA，也不会调用写卡器：

```text
状态查询
  ↓
筛选 esimProfileStatus = RELEASED 且查询成功
  ↓
手动勾选或自动选择前 N 张（最多 200 张）
  ↓
生成 JSON/CSV 批次清单
```

批次中的每张卡初始状态是 `lpa_status=not_requested`、`write_status=not_started`。只有用户明确点击一次性按钮后才会获取二维码/LPA；`INSTALLED` 和查询失败的卡不能加入批次。

也可以对已经保存的状态 JSON 使用命令行准备批次：

```powershell
.\.venv\Scripts\python.exe nexsim_profile_writer.py --input .\outputs\esim-status-raw-YYYYMMDD-HHMMSS.json --count 200
```

也可以从统一入口执行：

```powershell
.\.venv\Scripts\python.exe nexsim_status_tool.py prepare-write-batch --input .\outputs\esim-status-raw-YYYYMMDD-HHMMSS.json --count 200
```

Linux / macOS：

```bash
python3 nexsim_profile_writer.py --input ./outputs/esim-status-raw-YYYYMMDD-HHMMSS.json --count 200
```

批次文件保存在 `outputs/write-batches/`。生成批次时不会触发二维码接口，也不会写入 `activationCode`、LPA 或二维码内容。

### 一次性获取 LPA 和二维码

批次清单生成后，页面会显示“一次性获取 LPA + 二维码”按钮。创建批次本身不会触发二维码接口；只有用户明确点击并确认后才会开始：

```text
重新登录
  ↓
每张卡只请求一次 /api/inventory/{inventoryId}/qr-code
  ↓
校验 cardId、ICCID、READY、activationCode
  ↓
本地生成二维码 PNG
```

每张卡在请求二维码前会先重新调用 Profile 状态接口；只有复核仍为 `RELEASED` 才会请求二维码。`INSTALLED`、`failed` 或其他原始值都会跳过，不消耗二维码查看次数。这一步没有自动重试。超时、网络中断或非 JSON 响应都按“结果未知”处理，并停止后续卡，避免同一张卡再次消耗查看次数。LPA 只存在当前 Web 任务内存中，不写入批次 JSON/CSV；二维码 PNG 保存到对应批次的 `qr/` 目录。

当前还没有接入具体 eUICC 写卡器的命令行、SDK 或 HTTP API，因此任务完成后是“拿到 LPA + 生成二维码”，不是已经写入实体卡。后续接入写卡器时，应在同一个一次性任务里把 LPA 直接交给写卡器，并在完成后重新查询 Profile 状态确认 `INSTALLED`。

## 平台批量开户激活（独立页面）

`/activation.html` 与 Profile 状态查询是两个独立页面。平台侧开户不代表实体 eUICC 写卡：

```text
选择已准备批次
  ↓
只读预检：库存资格、重复订单、产品、单价、总金额、激活余额
  ↓
人工复核批次数量和金额，并再次确认
  ↓
一次性提交平台 CSV 激活请求
  ↓
轮询批量任务
  ↓
只读核验订单、号码和信号状态
```

提交前先识别账号，由工具取得组织 ID，再选择可开户套餐并填写本次授权金额上限。账号、组织和套餐必须与批次一致；总金额不能超过授权上限或激活余额。系统在外部提交前使用排他方式创建 `intent.json`，同一批次只能尝试提交一次；网络超时或响应未知时只允许执行结果核验，禁止自动重发。

新平台开户批次保存在 `outputs/platform-activation-batches/<pb-批次编号>.json`，开户文件保存在同目录的 `<pb-批次编号>/platform-activation/`，包括提交意图、CSV、平台回包、任务状态和核验结果。旧 `wb-` 批次仍保留在 `outputs/write-batches/`。核验 CSV 可从已保存记录下载，不会因此访问平台。

账号识别成功后，历史列表按“后台地址、组织、平台返回的账号名称”匹配。新批次记录这些归属信息，不保存密码、登录令牌或 LPA 原文。同组织但不同账号的新批次不会混在一起。切换账号会立即清空旧批次详情、下载链接和预检结果，重新识别后加载对应记录；刷新页面或重启服务后，也需要重新识别账号。

一次性任务逐张保存处理状态、时间、错误与文件摘要，结束后保存整批结果。重新选择批次时可查看逐卡历史状态，不会重新请求二维码或读取二维码图片。LPA 原文只存在原任务内存中，不能从历史记录恢复。中断且没有完成记录的任务显示为待核查，保持禁止重复获取。

早期批次没有账号归属字段。能够确认组织的旧 `pb-`、`wb-` 批次单独列在“同组织旧记录（未记录账号，仅供查看）”，不自动归属当前账号，也不能从该记录触发获取或开户操作；无法确认组织的旧记录不展示。现有本地服务仍需部署在受控环境，历史筛选不替代整站访问认证。

相关 Web API：

- `GET /api/write-batches`
- `POST /api/platform-activation/preview`
- `POST /api/platform-activation/submit`
- `POST /api/platform-activation/verify`
- `GET /api/platform-activation-jobs/{job_id}`
- `GET /api/write-batches/{batch_id}/platform-activation/verification.csv`

这组接口不会自动运行。必须由激活页面的对应按钮明确触发；页面加载、切换页面或刷新批次不会提交激活。

相关 Web API：

- `POST /api/write-batches`
- `GET /api/write-batches/{batch_id}`
- `GET /api/write-batches/{batch_id}/export.csv`
- `POST /api/activation-jobs`
- `GET /api/activation-jobs/{job_id}`
- `POST /api/activation-jobs/{job_id}/cancel`
- 查询完成后，页面顶部的“加载上次结果”按钮可以直接恢复最近一次已保存的数据；加载历史不会重新登录，也不会访问 NexSim。
- 历史结果会显示本次查询使用的账号名（例如“长工”或“pddhuhu”），只保存账号名，不保存密码；旧格式历史文件会显示“未知账号（旧记录）”。
- 历史 JSON/CSV 默认保存在服务端 `outputs/` 目录；使用 `--output-dir` 时保存到指定目录。

Profile 查询使用受控并发，默认同时查询 2 张卡；临时错误重试时会继续降低并发。可通过服务参数调整：

```bash
python3 nexsim_status_tool.py web --profile-workers 2
```

允许范围是 1—16。后台接口出现限流时应降低并发数，而不是无限增加。

## 状态含义

最近一次完整查询实际返回三种 Profile 原始状态：

- `INSTALLED`：已写入；
- `RELEASED`：尚未写入；
- `ERROR`：Profile 异常。

历史记录中还曾出现 `UNAVAILABLE`。工具会保留平台返回的任何新原始值；`failed` 是工具自己的查询失败标记，不是接口的 `data.status` 值。

后台专用接口是：

```text
GET /api/inventory/page
GET /api/inventory/{inventoryId}/esim-usage-status
```

其中 `inventoryId` 是库存记录的数字 `id`，不是 ICCID。Profile 查询固定以库存 `status=USED` 确定已成功开卡范围，再用 `esimProfileStatus` 判断是否写入；`ALLOCATED` 只属于独立的平台开户流程，不进入 Profile 统计或明细。

如果平台未来返回新的原始 Profile 值，页面会显示原始值，不会偷偷归类成“其他”。

## 安全边界

状态查询和“生成写卡批次清单”不会调用二维码接口，也不会读取或保存 `activationCode`/LPA。只有用户在页面中明确点击并确认“一次性获取 LPA + 二维码”后，独立的一次性任务才会逐张调用二维码接口。

Profile 状态查询页不会：

- 自动触发、重试或重复调用二维码接口；
- 读取平台的二维码剩余查看次数；
- 创建订单、激活、续订、销毁或恢复；
- 查询订单、号码或信号；
- 在批次 JSON/CSV 或日志中保存 LPA；
- 在未接入写卡器 SDK/CLI 的情况下声称已经写入实体 eUICC。

状态查询除登录使用一次 POST 外，只执行库存分页和 Profile 状态 GET。账号密码只在服务端内存中用于当前任务，任务结束后清掉，不写入结果文件。二维码 PNG 仅在用户明确确认一次性任务后保存到对应批次目录。

独立的平台激活页可以在用户明确操作后执行订单预检、一次性提交和结果核验，但不会自动提交、自动重试提交、续订、销毁或恢复，也不会替代实体 eUICC 写卡器。

## 命令行模式

命令行适合定时任务或批处理：

```powershell
.\.venv\Scripts\python.exe nexsim_status_tool.py --config nexsim-status.json status
```

复制 `nexsim-status.example.json` 为自己的配置。`org_id` 可填 `null`，工具会从登录结果自动识别；`profile_workers` 可设置为 1—16。
