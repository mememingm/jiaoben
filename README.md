# 本次 NexSim 采集与激活

本次使用 `oneoff.py`，通过网站真实使用的接口操作。
网页登录凭证只从用户指定的 `C:/Users/27792/Desktop/pddhuhu.txt` 读取，不复制到脚本、不打印密码。

## 本次已核对（2026-09-21）

- 网站：`https://admin.nexsimus.com/`。
- 开户客户：pddhuhu（老平台），组织 ID 564。
- 套餐：有信号短信卡，30 天、30 分钟、100 条短信，无流量。平台商品 `P_VOICE_SMS_30M_100`，网站商品 ID 7。
- 开户人姓名留空，使用网站 CSV 流程的默认随机地址。
- 目标 496 张；实际可用库存 274 张，缺 222 张。占用卡、已用卡、下级客户库存未纳入。
- 实时页面显示单价 3.50；274 张为 959.00 开卡余额单位。提交前再次读取价格和余额。
- 274 张真实二维码已经采集，尚未提交任何创建或激活订单。

## 输出

均位于 `C:/Users/27792/Desktop/esim二维码/er`：

- `eSIM二维码_<ICCID>.png`：274 张，一卡一图。
- `开户激活_274.csv`：与原模板一致，只有 ICCID 一列；原模板未覆盖。
- `激活核对表.xlsx`：ICCID 为字符串，单元格文本格式 `@`。包含 ICCID 清单与激活核对两页。
- `.nexsim-live`：本次固定清单、图片校验值，以及执行后生成的提交回包和核对证据。

CSV 无法保存 Excel 单元格格式。导入平台请直接使用 CSV；人工查看、核对请打开 XLSX。不要用 Excel 双击 CSV 后再保存，19 位号码可能永久丢失后几位。

## 运行

在 PowerShell 中进入 `D:/esim/nexsim-batch`，使用已准备好的环境：

```powershell
Set-Location D:\esim\nexsim-batch
```

只查库存与价格：

```powershell
.\.venv\Scripts\python.exe -X utf8 oneoff.py inspect
```

**下面会实际扣费，需先确认按现有 274 张执行、金额上限 959.00：**

```powershell
.\.venv\Scripts\python.exe -X utf8 oneoff.py activate --count 274 --max-total 959.00
```

脚本用网站原生 CSV 批量接口提交固定清单，后台处理期间查询任务进度，最后再查询每个 ICCID 的订单与号码记录。不会仅凭“请求成功”或批次成功数量判定完成。

只读回查，无扣费：

```powershell
.\.venv\Scripts\python.exe -X utf8 oneoff.py verify
```

成功必须同时满足：所属客户和套餐正确、订单状态 `ACTIVE`、号码状态 `ACTIVE`、订单和号码的信号状态 `SUCCESS`、ICCID/卡 ID/订单 ID/手机号对应一致、二维码文件完整。结果写入 XLSX 和 `激活结果.csv`，原始对应回包及北京时间保存到本次目录。

提交超时或进程中断时，保留 `.nexsim-live`，不要删除它后重跑。脚本在发请求前写入标记；已尝试的批次只查询，不自动重发，避免重复开卡。缺失 222 张不自动取用其他客户或已激活卡，需先明确目标口径或补货。

网站当日维护公告列出 9 月 21 日 16:10—18:20 窗口，正式执行前按页面最新说明核对。

`nexsim_batch.py` 是最初依据开放 API 文档编写的版本；本次实际操作入口为上述 `oneoff.py`，不需要另外申请开放 API 密钥。
