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

页面中输入后台账号和密码，点击“获取 eSIM 状态”。组织 ID 会从登录结果自动识别。

服务默认只监听本机。如果要让同一局域网的其他设备访问：

```bash
python3 nexsim_status_tool.py web --host 0.0.0.0 --port 8765
```

局域网或公网部署时，应在反向代理后增加 HTTPS、访问认证和防火墙规则。当前内置服务适合本机或受控内网使用，不是完整的公网身份认证系统。

## 查询和筛选

- 查询范围可选“全部 eSIM”或“指定 ICCID”；
- 查询过程中显示整体进度、当前阶段和百分比；
- 结果可按 ICCID 或原始字段搜索；
- 可筛选“已安装（INSTALLED）”；
- 可筛选“已释放待下载（RELEASED）”；
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

Profile 查询使用受控并发，默认同时查询 8 张卡，减少逐张等待。可通过服务参数调整：

```bash
python3 nexsim_status_tool.py web --profile-workers 4
```

允许范围是 1—16。后台接口出现限流时应降低并发数，而不是无限增加。

## 状态含义

当前已确认的 Profile 状态入口有三类：

- `INSTALLED`：后台接口原始值，页面显示“已安装”；
- `RELEASED`：后台接口原始值，页面显示“已释放待下载”；
- `failed`：工具自己的查询失败标记，不是后台接口的 `data.status` 值。

后台专用接口是：

```text
GET /api/inventory/page
GET /api/inventory/{inventoryId}/esim-usage-status
```

其中 `inventoryId` 是库存记录的数字 `id`，不是 ICCID。库存 `status` 与 Profile `esimProfileStatus` 是两套不同状态，都会保留。

如果平台未来返回新的原始 Profile 值，页面会显示原始值，不会偷偷归类成“其他”。

## 安全边界

工具不会：

- 点击或查询二维码；
- 下载或生成激活码；
- 读取二维码查看次数；
- 创建订单、激活、续订、销毁或恢复；
- 查询订单、号码或信号。

除登录使用一次 POST 外，后台只执行库存分页和 Profile 状态 GET，不执行 POST、PUT、DELETE 业务写操作。账号密码只在服务端内存中用于当前任务，任务结束后清掉，不写入结果文件。

## 命令行模式

命令行适合定时任务或批处理：

```powershell
.\.venv\Scripts\python.exe nexsim_status_tool.py --config nexsim-status.json status
```

复制 `nexsim-status.example.json` 为自己的配置。`org_id` 可填 `null`，工具会从登录结果自动识别；`profile_workers` 可设置为 1—16。
