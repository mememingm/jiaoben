# NexSim Profile 状态与平台开户工具

这是一个本地 Web 工具，包含两套彼此独立的业务流程：

- **Profile 状态查询**：查询 eSIM Profile 状态，展示 `INSTALLED`、`RELEASED` 和查询失败，并保存每次查询记录。
- **平台开户激活**：从平台库存选择 ICCID、建立固定批次、按需获取 LPA/二维码，再执行开户预检、提交和结果核验。

平台开户不会读取或依赖 `esimProfileStatus`。Profile 页面也不会再创建平台开户批次。

## 本地运行

需要 Python 3.10 或更高版本。

Windows PowerShell：

```powershell
Set-Location D:\esim\nexsim-batch
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 nexsim_status_web.py
```

Linux / macOS：

```bash
cd /path/to/nexsim-batch
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python nexsim_status_web.py
```

启动后访问：

- Profile 状态：`http://127.0.0.1:8765/`
- 平台开户注册：`http://127.0.0.1:8765/activation.html`

默认只监听本机。只有明确需要局域网访问时才使用 `--host 0.0.0.0`，并自行配置防火墙和访问控制。

## 平台开户注册流程

1. 输入平台账号、密码、组织 ID 和产品 ID，读取平台库存。
2. 从 `ALLOCATED + ESIM + P` 且属于指定组织和产品的库存中选择 1 到 200 张卡。
3. 建立 `pb-*` 平台批次。批次会固定组织、产品和 ICCID 清单。
4. 人工确认后启动一次性二维码任务。每张卡最多请求一次，超时或结果未知不会自动重试。
5. 填写授权金额上限并执行只读预检，核对库存、重复订单、产品价格和余额。
6. 再次人工确认后提交开户激活；提交意图会先落盘，任何未知结果都禁止重发。
7. 使用结果核验读取订单、号码和信号状态，并导出核验 CSV。

选择已有批次后，组织 ID 和产品 ID 由批次锁定。后端也会拒绝与批次不一致的请求，避免对错误客户或套餐执行操作。

## 数据目录

默认输出目录为 `outputs/`：

- `esim-status-raw-*.json`：Profile 查询历史。
- `platform-activation-batches/pb-*.json`：平台批次元数据。
- `platform-activation-batches/pb-*.csv`：批次 ICCID 清单。
- `platform-activation-batches/<batch-id>/qr/`：已确认任务保存的二维码图片。
- `platform-activation-batches/<batch-id>/platform-activation/`：提交意图、平台回包和核验结果。
- `write-batches/wb-*`：旧版 Profile 写卡批次，仅保留兼容，不再由新页面创建。

账号密码只保存在服务端当前任务内存中，不写入浏览器存储、批次文件或日志。LPA 只在当前二维码任务结果中返回，批次文件不会保存明文 LPA。

## 主要接口

- `POST /api/jobs`：启动 Profile 状态查询。
- `GET /api/history`、`DELETE /api/history/<filename>`：查询和删除历史记录。
- `POST /api/platform-inventory-jobs`：读取平台库存。
- `GET|POST /api/platform-batches`：读取或建立平台批次。
- `POST /api/platform-qr-jobs`：显式启动一次性二维码任务。
- `POST /api/platform-activation/preview`：只读预检。
- `POST /api/platform-activation/submit`：一次性提交开户激活。
- `POST /api/platform-activation/verify`：只读核验结果。

## 安全边界

- 读取库存不会调用二维码接口。
- 二维码任务与开户提交都必须由页面上的独立按钮显式触发。
- 二维码请求和开户提交没有自动重试；未知结果只能核验，不能重发。
- 单批最多 200 张；不跨组织、不跨产品，也不自动取用不符合库存条件的卡。
- 不要把 `outputs/`、真实账号配置、二维码或平台原始回包提交到 Git。
