# NexSim 本地工具交接（当前状态）

更新时间：2026-09-30。项目目录：`D:\esim\nexsim-batch`；远端：`https://github.com/mememingm/jiaoben.git`。本文件是当前快照；根目录 `HANDOFF.md` 记录早期设计和多轮演变，其中关于未提交改动、当前基线与服务状态的描述已经过时。

## 1. 交付状态

- 当前分支 `main`，检查时 `HEAD` 为 `a0644a2`，已推送至 `origin/main`，工作区无未提交改动。
- 三个入口：`/` 是 Profile 状态查询，`/activation.html` 是新卡平台开户，`/installation.html` 是已开卡 eSIM 按 ICCID 准备手动安装资料。
- 最近完成：手动安装页可全选/取消全选、显示已选数量；只读核对有分阶段进度；同一浏览器关闭后仍可打开新建任务的历史记录。
- 本次交接检查时，本机 8765 端口没有监听进程，服务**未运行**。不要依据前一次会话的“已启动”记录判断当前状态。
- 用户已要求不要做测试；最近的页面与历史功能没有运行测试或浏览器交互验证。更早的 21 项离线检查通过，发生在这些功能加入之前，不能作为当前版本的验收结论。没有执行真实平台资料获取或平台开户。

## 2. 业务概念与用户流程

`USED` 表示平台侧卡已开通；Profile 的 `RELEASED` 表示尚未确认安装到设备。二者组合是手动安装流程的候选条件，不等于平台保证可以重新取得 LPA，也不等于 Profile 已安装。`INSTALLED` 才是 Profile 已安装的查询结果。

1. 状态页按安全的库存和 Profile 状态接口查询；可把筛选出的 `USED + RELEASED` ICCID 带入手动安装页。
2. 手动安装页“只读核对 ICCID”登录后读取当前组织 `USED` eSIM 库存、库存中的查看次数与每张卡的 `esim-usage-status`。**这一步不调用 `/qr-code`、不生成 LPA、不开卡。**进度先显示登录/库存读取的等待状态，再显示逐卡及历史标记核对。
3. 用户勾选通过核对的卡，点击“获取所选卡的安装资料”并确认后，后端重新核对账号、库存、Profile 与历史标记，才可能调用一次性 `/qr-code`。此调用可能消耗查看次数；失败或结果未知时不自动重试。该页面不会创建订单或提交平台开户。
4. 成功后资料保存在本机；用户下载已有 PNG 或 LPA 文件并自行在设备安装。下载本地文件不会再次请求平台。安装后由用户返回状态页发起复查。
5. 新卡开户注册页是独立流程，可能获取资料、提交订单和扣费；不要把该页的激活状态与 Profile 安装状态混为一谈。

## 3. 关键代码与接口

| 位置 | 作用 |
| --- | --- |
| `nexsim_status_web.py` | HTTP 路由、三个页面、后台任务管理与本地文件下载。 |
| `nexsim_status_checker.py` | 已开卡 Profile 状态查询与结果保存。 |
| `nexsim_installation.py` | ICCID 只读核对、逐卡进度、历史索引、显式获取及中断恢复。 |
| `nexsim_material_guard.py` | 以平台 host、组织及库存 ID 建立跨流程独占请求标记。 |
| `nexsim_platform_batch.py` / `nexsim_platform_activation.py` | 新卡批次、资料获取、开户预检、提交及核验。 |
| `web/installation.html`, `web/installation.js`, `web/styles.css` | 手动安装页及交互。 |

手动安装页主要接口：`POST /api/installation/prepare` 只读核对；`GET /api/installation-jobs/<job-id>` 读取任务；`POST /api/installation/fetch` 是敏感的显式资料请求；`POST /api/installation/cancel` 停止后续卡；`GET /api/installation/history` 读取当前浏览器历史；`POST /api/installation/history/attach` 将本标签页原任务加入历史。`GET /api/installation-jobs/<job-id>/files/<inventory-id>.txt|png` 仅下载已保存的本地资料。**Agent 不得调用获取或下载资料的接口做探测或演练。**

## 4. 本地数据与恢复

- 默认 `outputs/` 由 `.gitignore` 排除，但它是业务恢复所需的数据目录，迁移或重装前应整体备份、限制访问。不要提交、上传或公开它。
- `outputs/installation-batches/im-<随机任务 ID>/record.json` 仅保存任务元数据、核对结果与进度；`materials/<inventory-id>.txt/.png` 是敏感安装资料。
- `outputs/installation-history/` 保存由浏览器 HttpOnly、SameSite=Strict、最长一年 Cookie 关联的任务 ID 索引。清除本站 Cookie 会失去历史入口，但不会删除服务端资料。旧版本中已关闭标签页的任务不会自动进入新索引。
- `outputs/installation-attempts/` 是跨手动安装、新卡批次与旧写卡流程的防重复请求标记。已有标记或历史获取记录时，先核查已有资料与平台状态；不可为了补资料删除标记并重试。
- 服务重启后任务记录可读，已保存资料可下载；正在核对或获取的任务不会续跑。重启前虽核对通过但尚未获取的任务仅供历史查看，要继续需重新只读核对。
- `outputs/esim-status-raw-*` 是 Profile 查询历史；`outputs/platform-activation-batches/` 包含新卡批次、二维码与开户证据；`outputs/write-batches/` 是旧流程记录。这里可能有真实 ICCID、回包和安装资料，排查时先读元数据，避免展开敏感内容。

## 5. 安全边界

本用户已明确要求：Agent 在修改、排查、验证或交接时**不得查看、获取、下载、生成真实 eSIM 二维码或 LPA，也不得提交真实开户激活**。不要点击“查看二维码”“获取所选卡的安装资料”“按 ICCID 激活卡”，不要调用对应接口进行试探。状态查询只能使用已确认不会触发查看次数的库存/Profile 只读路径；证据不足时停止并说明。用户自行操作页面的敏感步骤与 Agent 的工作边界要区分。

任务 ID 是可用于读取批次详情及已保存文件的随机访问凭据；历史 Cookie 与任务 ID 均需保护。本服务默认仅监听 `127.0.0.1`，没有完整的多用户认证，不能直接暴露到局域网或公网。账号密码不得写入文档、日志或 Git。

## 6. 启动与接手顺序

Windows PowerShell（依赖已安装时）：

```powershell
Set-Location D:\esim\nexsim-batch
.\.venv\Scripts\python.exe -X utf8 nexsim_status_web.py --host 127.0.0.1 --port 8765
```

如需安装依赖，先在本机执行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。默认结果目录是项目内 `outputs/`。确认端口未被其他实例占用再启动；不要以删除 `outputs/` 的方式解决启动问题。

接手时先看 `git status --short`、当前服务进程与端口，再读本文件、`README.md`、`docs/manual-installation.md`。下一步优先用脱敏、离线数据审阅历史记录与进度条的边界行为；用户当前“不做测试”的指令仍应遵守，除非用户明确改变要求。真实平台是否允许对 `USED + RELEASED` 卡再次取得有效安装资料尚未验证，不能对用户承诺必然可取回。
