# NexSim Profile 状态与平台开户工具

已开卡、待安装的卡可使用独立的 [按 ICCID 准备手动写入](docs/manual-installation.md) 页面：`/installation.html`。支持从查询结果带入或粘贴 ICCID、只读核对、明确确认后获取安装资料、下载已保存资料和安装后复查；不会再次提交开户。

这是一个本地 Web 工具，包含三套彼此独立的业务流程：

- **Profile 状态查询**：查询 eSIM Profile 状态，展示 `INSTALLED`、`RELEASED` 和查询失败，并保存每次查询记录。
- **已开卡手动安装资料**：按 ICCID 只读核对已开卡库存，经用户明确确认后准备设备安装资料。
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
- 已开卡手动安装：`http://127.0.0.1:8765/installation.html`
- 平台开户注册：`http://127.0.0.1:8765/activation.html`

默认只监听本机。只有明确需要局域网访问时才使用 `--host 0.0.0.0`，并自行配置防火墙和访问控制。

## 项目文件

- `nexsim_status_web.py` 与根目录的 `nexsim_*.py`：Web 服务、状态查询、安装和平台批次逻辑。它们互相导入，直接从项目根目录运行。
- `nexsim_admin_batch.py`：独立的管理后台命令行工具，使用方式见[通用后台脚本说明](docs/README-通用后台脚本.md)。
- `web/`：三个页面及共用样式、动效；`tests/`：离线检查。
- `docs/`：[交接快照](docs/current-handoff.md)、[手动安装](docs/manual-installation.md)、[开户套餐](docs/activation-products.md)、[状态工具说明](docs/README-eSIM状态查询工具.md)和[通用后台脚本说明](docs/README-通用后台脚本.md)。
- `outputs/`：本地业务记录和敏感安装资料，不受 Git 跟踪，迁移前需单独备份。
- `.venv/`、`__pycache__/`：本机依赖或缓存，不提交到 Git。

## 平台开户注册流程

1. 输入平台账号和密码，点击“识别账号并加载套餐”。工具从登录结果自动识别组织 ID，并读取该组织的可开户套餐。
2. 从下拉菜单选择套餐，再读取平台库存。列表使用平台 P001—P004 / A001 编号，显示接口提供的套餐规格和单价，只展示启用开户的基础套餐，排除叠加包。当前工具仅支持 P 类库存，A 卡套餐会禁用。
3. 从 `ALLOCATED + ESIM + P` 且属于自动识别组织和所选套餐的库存中选择 1 到 200 张卡。
4. 建立 `pb-*` 平台批次。批次会固定组织、产品和 ICCID 清单。
5. 人工确认后启动一次性二维码任务。每张卡最多请求一次，超时或结果未知不会自动重试。
6. 获取资料完成后，自动准备激活并弹出卡数、实际总价确认框；确认后按当前批次 ICCID 提交激活，随后自动查询结果。
7. 已获取资料的历史批次可点击“激活并检查”，无需重新获取二维码；提交过的批次只允许“重新检查”。

激活条件检查在后台自动完成；激活前需填写“本次授权金额上限”，总价超过上限时停止提交。用户确认总价后才提交激活；“重新检查”只查询，不提交。无法确认时显示“未确认”，不直接判断为未激活。

选择已有批次后，组织和套餐由批次锁定。后端会重新登录并拒绝账号组织与批次不一致的请求，避免对错误客户或套餐执行操作。

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
- `POST /api/platform-context-jobs`：自动识别账号组织并读取可开户套餐。
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

套餐编号映射、筛选依据与当前支持范围见 [开户套餐说明](docs/activation-products.md)。套餐列表升级后需重启本地服务、刷新页面并重新识别账号。
