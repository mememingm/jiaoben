# NexSim 通用后台批量工具

`nexsim_admin_batch.py` 面向 NexSim 管理后台账号，按以下顺序工作：

```text
doctor/inspect（只读检查）
        ↓
collect（固定库存、下载二维码、生成 CSV）
        ↓
activate（唯一扣费写操作）
        ↓
verify（逐张确认订单、号码、信号）
```

这是管理后台接口工具，不使用公开 Open API 的 `clientId/clientSecret`。使用者必须拥有平台账号和对应组织的操作权限。

## 准备

在 `nexsim-admin.example.json` 的基础上复制出自己的 `nexsim-admin.json`，修改 `org_id`、客户名称、套餐、数量和目录。`product_id` 可以填 `null`，脚本会按 `product_code` 查找；如果平台存在同编码的多个内部商品，再填写平台确认的 `product_id`。不要把密码写入 JSON。

凭据文件必须是两行纯文本：

```text
第一行：登录账号
第二行：登录密码
```

把凭据文件和 `nexsim-admin.json` 放在本机，不要发到群里或提交到 Git。

安装依赖并进入脚本目录：

```powershell
Set-Location D:\esim\nexsim-batch
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

脚本使用 Python 标准库生成文本格式 XLSX，不依赖特定电脑上的 Node、Codex 或 Excel 安装。

## 使用

先只读检查登录、套餐和可用库存：

```powershell
.\.venv\Scripts\python.exe nexsim_admin_batch.py --config nexsim-admin.json doctor
.\.venv\Scripts\python.exe nexsim_admin_batch.py --config nexsim-admin.json inspect
```

采集二维码和 CSV。这一步不会创建订单，也不会扣费：

```powershell
.\.venv\Scripts\python.exe nexsim_admin_batch.py --config nexsim-admin.json collect
```

激活前必须明确金额上限，并显式确认扣费：

```powershell
.\.venv\Scripts\python.exe nexsim_admin_batch.py --config nexsim-admin.json activate --max-total 1050.00 --confirm-activation
```

只读核对结果：

```powershell
.\.venv\Scripts\python.exe nexsim_admin_batch.py --config nexsim-admin.json verify
```

机器调用可加 `--json`。错误也会返回 JSON，但不会输出密码、令牌或完整二维码激活码。

## 成功判据

一张卡只有同时满足以下条件才算成功：

- 订单状态为 `ACTIVE`；
- 号码状态为 `ACTIVE`；
- 订单和号码的信号状态都为 `SUCCESS`；
- ICCID、卡 ID、订单和手机号对应一致；
- 二维码文件存在且哈希未改变。

`PENDING` 表示平台仍在处理信号，`ACTIVATION_UNKNOWN` 或“结果待确认”不能算成功，也不能自动重新提交。

## 输出文件

- `eSIM二维码_<ICCID>.png`：二维码图片；
- `开户激活_<数量>.csv`：上传平台的 CSV；
- `激活结果.csv`：逐卡核对结果；
- `激活核对表.xlsx`：ICCID 按文本格式保存的查看表；
- `.nexsim-batch/`：固定清单、提交回包、查询证据和锁文件。

平台导入时直接使用 CSV。不要用 Excel 打开 CSV 后重新保存，否则 19 位 ICCID 可能变成科学计数法或丢失末位。

## 安全边界

脚本会在提交前固定 ICCID 清单，并在写操作前保存 `activation-intent.json`。网络超时、进程中断或平台返回结果待确认时，后续运行只允许 `verify`，不会自动重发。

不同组织应使用不同的 `output_dir`。不要把一个组织的输出目录换给另一个账号继续使用。
