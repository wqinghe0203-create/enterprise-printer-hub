---
name: enterprise-printer-hub
description: 企业多打印机状态查询、实时作业查看、文件打印和任务调度技能。用于“添加打印机”“接入网络打印机”“谁在打印”“打印机列表”“查看27楼队列”“打印这份文件”“紧急插队”“取消打印”“打开打印后台”“Word 或 Excel 转换可用吗”及打印机网络诊断。通过本技能自带 Python CLI 和网页工作台操作 config.json 中的设备。
metadata:
  version: "0.2.2"
---

# 企业智能文印中心 · WorkBuddy 技能

本目录就是完整技能根目录，包含 CLI、网页 UI、配置和 Windows 启动器。部署与依赖安装见 [README.md](README.md)。保持目录名 `enterprise-printer-hub`，不要只复制本文件。

## 首次调用与打开控制台

首次使用或用户要求打开控制台时，先发一条可见进度：“我正在检查打印环境。PDF 和图片所需的依赖就绪后，我会打开打印后台。”然后在实际技能根目录运行对应入口，读取最后一行 JSON。

**Windows 打开控制台时使用 WorkBuddy 管理的 PowerShell 后台任务。** 从本技能实际安装目录执行：

0. 先在普通 PowerShell 调用中运行 `powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1 --check-console --no-wait`。若返回 `console_verified: true`，直接回复控制台地址并结束。只有端口未运行服务时才继续第 1 步，端口被其他服务占用时提示冲突。

1. 在普通 PowerShell 调用中运行 `powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1 --install-only`，确认依赖配置完成。配置失败时直接报告结构化错误，结束本次调用。使用管理员指定的离线目录或 pip 源，勿自行切换镜像、调整代理或手动追加 pip 命令。
2. 使用 PowerShell 工具的 `run_in_background: true` 参数运行 `powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1 --serve`。该命令保持运行，由 WorkBuddy 管理。保留返回的后台任务编号。不要等待服务退出，也不要结束该任务。
3. 另起一次普通 PowerShell 调用运行 `powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1 --check-console`。只有返回 `console_verified: true` 才能报告服务就绪，并给出返回的地址。失败时查看上一步后台任务输出，报告实际错误。浏览器打开请求成功只代表已请求系统打开。

macOS 继续运行：

```sh
/bin/zsh ./scripts/bootstrap-mac.sh --first-use
```

macOS 用户明确要求打开控制台时省略 `--first-use`。仅在 `console_verified: true` 时报告服务就绪。浏览器未打开时提供返回地址。等待配置入口完成并读取最后一行 JSON，首次配置 Windows 依赖最多等待 90 秒。启动失败时报告返回的原因；不要直接运行网页脚本或用浏览器开页代替服务就绪检查。Windows 已就绪但页面失败时，另起一次只读请求核查返回地址的 /api/local-ready，核查失败就明确说明服务未在运行。Word 和 Excel 的远程转换结果分别显示在本机控制台。缺少服务配置时，提示联系管理员或改传 PDF。若返回 `busy`，报告正在配置；若返回 `failed`，按 `phase` 和 `message` 告知失败原因。端口冲突可用 `--port 18829`。

用户询问 Word 或 Excel 转换状态时，使用技能 Python 执行 `scripts/bootstrap.py --office-status`。用户要求复检时使用 `--retry-office`，它只发送内置样例。控制台运行时可用 `--office-status` 查询后台进度。根据返回的 `word`、`excel` 和各自的消息报告结果。

仅打开网页、查询设备或记录的请求不创建任务。收到明确打印请求后使用原有 CLI。Office 转换未配置或失败时，按 CLI 返回原因告知用户，并提示可改传 PDF。检查结果仅用于诊断，以当前任务状态为准。

## 给用户的结果回复（默认中文表格）

面向普通员工，最终回复使用“一句中文结果 + 五项信息表格”。表格直接渲染，不放在代码块里，不复制 CLI 原始长输出。下面是已确认完成的示例（文件名、编号与人员须替换为本次真实任务，不照抄示例）：

**设备已确认完成。**

| 项目 | 内容 |
|---|---|
| 打印文件 | test---6.docx |
| 打印机 | 佳能 C3130L（27F 总部） |
| 打印设置 | 1页 · 1份 · 单面 |
| 提交人 | 张三 |
| 任务编号 | #105 |

### 顶部结果只展示一个真实阶段

以下状态名仅供内部判断，普通回复只使用右侧中文，不展示英文状态码：

| 观察到的实际记录 | 给用户的顶部结果 |
|---|---|
| completed 且 completion_verified=true | 设备已确认完成。 |
| submitted | 已提交，等待设备确认。 |
| queued | 已排队，等待发送到打印机。 |
| printing | 设备正在打印。 |
| unknown | 打印结果暂未确认，请勿重复提交。 |
| failed | 未能打印：简短中文原因。 |
| converting | 正在处理文档，尚未发送到打印机。 |
| sending | 正在发送到打印机，请勿重复提交。 |
| canceled | 任务已取消。 |
| paused | 任务已暂停。 |
| unconfirmed，或 completed 缺少完成确认 | 打印结果尚未核实。 |

### 表格内容与回复边界

- 默认只保留打印文件、打印机、打印设置、提交人、任务编号五项。打印机名称后附位置；文件行只写文件名。页数、份数和单双面合并为“打印设置”，不在其他行重复。
- 设置使用日常中文：simplex 写“单面”，duplex_long 写“长边双面”，duplex_short 写“短边双面”，auto 写“按打印机默认设置”。不贴代码、布尔值或参数名，不将设备默认擅自解释为已知单双面模式。
- 只使用确知的信息；某个设置未知时省略这一段，整项信息缺失时省略对应行。不猜测页数、份数、提交人、设置或设备，不把文档页数当成实际出纸张数。
- 失败原因转换成普通员工能理解的一句话，例如“打印机离线”“文件转换失败”“设备不支持双面打印”。未知原因写“暂未取得明确原因”，不原样堆叠异常栈和协议字段。
- 只有实际记录为 completed 且 completion_verified=true 才说“设备已确认完成”；不额外宣称人工确认取纸。sheets_completed=0 本身不推翻硬件 job-state=9 的完成确认，不据此声称没有出纸、要求用户每次手工确认或重新打印。
- 不同时说“已提交”和“已完成”。失败或结果未知时，表格后最多补充一句必要建议；普通成功回复到表格结束，不主动追加“另外两件事”、过程说明或长篇注意事项。
- 普通反馈不展示 IPP、Validate-Job、PWG Raster、hardware_job_id、completion_verified、sheets_completed、数据库路径、转换产物路径、解释器或开发目录。技术细节只在用户明确追问或诊断需要时按需解释，不扩充默认表格。
- 目录副本问题只在确实影响本次操作时用一句话提示，方案等用户追问。用户只要求提交打印时，不为凑齐最终 completed 状态反复运行全设备列表 / 诊断或长期等待；报告观察到的真实阶段即可。明确要求确认完成时再查询对应任务，禁止重投文件。
- 转换产物只在用户明确要求删除且确认未被活动任务使用时清理；不自动删除原件、历史记录或其他任务文件。不因修改反馈模板改变打印程序、状态判定、文件保留或数据目录。

## 运行环境与定位

优先使用当前已加载 SKILL.md 所在目录，不依赖开发者电脑路径。Windows 常用安装位置为 `%USERPROFILE%\.workbuddy\skills\enterprise-printer-hub`；若 WorkBuddy 实际加载路径不同，以实际目录为准。

同一次操作的 CLI、配置、队列和网页必须来自同一安装目录。发现网页与当前技能来自不同副本时，说明记录不共享，不擅自改用开发目录、建立 junction、合并数据库、停服务或迁移文件。单独运行 CLI 也会主动尝试调度，不能仅因没有常驻网页消费者就断言“永远不会打印”。目录统一与数据迁移需要先得到用户明确授权。

使用 PowerShell 时，可按下例探测解释器。`$SkillRoot` 必须指向本技能实际安装目录：

```powershell
$SkillRoot = Join-Path $env:USERPROFILE '.workbuddy\skills\enterprise-printer-hub'
$Candidates = @(
  (Join-Path $SkillRoot '.venv\Scripts\python.exe'),
  (Join-Path $env:USERPROFILE '.workbuddy\binaries\python\envs\default\Scripts\python.exe')
)
$PythonExe = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $PythonExe -and (Get-Command py -ErrorAction SilentlyContinue)) {
  $PythonExe = & py -3 -c 'import sys; print(sys.executable)'
}
if (-not $PythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
  $PythonExe = & python -c 'import sys; print(sys.executable)'
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) { throw '未检测到 Python，请先运行安装依赖.bat 或安装 Python 3.9+' }
& $PythonExe -c 'import sys; print(sys.executable); assert sys.version_info >= (3, 9), "需要 Python 3.9+"'
```

从其他电脑复制来的 `_local.json` 可能含无效路径，不直接信任其中的解释器。首次配置使用上面的平台入口；Windows 用户也可手动运行根目录的 `安装依赖.bat`。后续 CLI 和 Web 使用本目录的同一个 `.venv` 解释器。

macOS / Linux 使用技能目录的 `.venv/bin/python` 或有效的 `python3`，将下述 CLI 参数原样传入。PDF 光栅转换依赖 `PyMuPDF`。所有平台的 Office 转换使用管理员配置的内网 HTTPS 服务。配置和凭据保存在技能目录的 `data/office-converter.json`，具体设置见 README。转换后的 PDF 在投递前检查可见中文的缺失字形。发生缺字时请管理员检查服务端字体，或让用户上传显示正常的 PDF。

## 设备和队列

`config.json` 的 `printers` 是实际设备池，`system.default_printer_id` 决定默认设备。交付包保留总部佳能配置 `10.1.36.228`，同事须在能访问该设备的公司网络中使用，或修改配置接入自己的设备；配置不代表设备当前在线。

- 查询命令只读，不启动网页服务也可使用。
- 每次打印优先依据用户明确指定的位置、型号或设备 ID。未指定时使用默认设备。
- 未指定设备时使用有效的配置默认设备；指定设备时先匹配准确 ID，再匹配唯一完整名称，最后匹配名称 / 位置 / 型号关键词。未命中或存在多台候选会停止并提示，不能回退默认设备或随意取第一台。失效的默认设备或损坏配置也会停止，不使用内置设备代替。
- 当前队列在本机 `data/queue_state.sqlite3`，同目录网页与 CLI 共用事务队列；分别安装的副本不共享记录或调度锁，硬件作业查询仍来自打印机自身。SQLite 和技能目录应在本地磁盘，不放在网络共享目录。

## 添加网络打印机

用户要求新增设备时，在服务电脑上操作。网页进入“打印设备 → 添加打印机”，输入公司私网 IPv4；默认 IPP 端口 631、路径 /ipp/print，高级设置可修改。检测只读取能力，不打印测试页。检测成功后填写名称和位置，再确认保存。

对话也可调用 CLI（以下沿用前面的解释器和目录变量）：

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" add-printer --ip "10.1.36.229"
& $PythonExe "$SkillRoot\scripts\cli.py" add-printer --ip "10.1.36.229" --name "研发区打印机" --location "26F 研发区" --save
```

默认只检测并输出 JSON 能力；`--save` 在重新检测通过后才新增。用户明确要求添加并且名称/位置明确时可使用 `--save`，只询问连通性时不保存。支持 `--ipp-port` / `--ipp-path`；不要用编辑 config.json 绕过校验。

- 只接入与当前 PDF / PWG Raster 链路兼容的网络设备，不扫描局域网，不接入 USB 或系统驱动队列。
- 拒绝本机、公网、组播和非法地址；检测不跟随重定向。离线或格式不兼容时保留错误，不保存。
- 同一地址、端口、路径不能重复新增；设备 ID 由服务生成。新增不会更改默认设备、已选打印机、上传文件或历史任务。
- 配置使用 Windows / Mac 跨进程锁、原子替换和 config.json.bak 备份；损坏或写入失败时停止，不拿默认值覆盖原配置。更新技能时保留本机 config.json 和 data，不用发行包空队列覆盖已有记录。
- Web 添加接口仅允许本机 localhost / 127.0.0.1 同源请求；局域网访客需到服务电脑添加，既有局域网打印仍可使用。
- 网页可选择打印机默认、单面、长边双面或短边双面；默认选项读取设备实时 sides-default，缺失时使用单面。CLI 保留原有默认长边双面，用户可传 `--duplex auto` 使用设备默认设置。实际提交仍执行格式协商与 Validate-Job，不承诺接入即代表已实际出纸。

## 查询状态、谁在打印和队列

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" printers
& $PythonExe "$SkillRoot\scripts\cli.py" status --printer "27楼"
& $PythonExe "$SkillRoot\scripts\cli.py" queue --printer "27楼"
```

`printers` 查询全部设备；`queue` 同时显示硬件作业和本机虚拟队列。输出适合手机阅读：用紧凑指标和纵向设备/作业卡片，避免宽表格。保留真实的离线、错误和空队列状态，不把查询不到作业解释为打印成功。

## 打印用户提供的文件

用户明确要求打印时，从附件取得实际的本地文件路径，不构造不存在的路径。支持 PDF、DOC/DOCX、XLS/XLSX、JPG/JPEG/PNG/BMP；Office 原文件会通过 HTTPS 发送到管理员配置的公司转换服务。

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" print --file "C:\实际附件目录\文件.pdf" --user "张三" --printer "27楼" --priority 50 --duplex duplex_long --copies 1
```

- 未指定姓名使用 `普通用户`；普通优先级 50，高优先级 80，加急 100，取值 1–100。
- `duplex_long` / `duplex_short` / `simplex` 与 `copies` 会作为 IPP 作业属性发送，并在提交前执行 Validate-Job。设备不支持所选模式或份数时拒绝提交，不静默替换设置。PWG Raster 双面暂支持 sheet-back=normal，其他变换需使用系统驱动。
- 返回任务编号、目标设备和观察到的状态。提交成功只表示创建/提交任务，不等于确认纸张已输出；需要核实时再查询硬件队列/日志。
- 先查询设备的 document-format-supported：支持 PDF 才发送 application/pdf，否则转换为支持的 PWG Raster（当前支持 srgb_8 / sgray_8、72–600 dpi 方形分辨率）。不能以 application/octet-stream 绕过格式检查；不支持兼容格式时明确报错，不发送原始 PDF。
- 接收作业后保存 hardware_job_id 并标为 submitted；只有 Get-Job-Attributes 返回 job-state=9 才标为 completed。超时、未知响应标为 unknown，不自动重试或切换驱动补打；旧版 completed 记录显示 unconfirmed。网页刷新 / 状态查询会核对硬件终态。
- 用户要求打印即授权提交该文件；不要重复确认。出现超时、返回不确定或用户只反馈错误时，先核查硬件队列/历史，避免自动重投产生重复打印。

## 优先级与取消

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" cut-in --job-id 101
& $PythonExe "$SkillRoot\scripts\cli.py" priority --job-id 101 --priority 80
& $PythonExe "$SkillRoot\scripts\cli.py" cancel --job-id 101
& $PythonExe "$SkillRoot\scripts\cli.py" cancel --job-id 102 --printer "canon_c3130l_27f" --hw
```

`cut-in` / `priority` 调整本机虚拟队列的待调度顺序，不能抢占正在出纸的硬件作业。虚拟 ID 和硬件 ID 不混用；取消硬件作业加 `--hw` 并使用准确的设备 ID。未给任务 ID 时先查询队列定位，不凭姓名猜测取消多个任务。网页服务运行时有持续后台消费器，按设备串行转换与投递；仅 CLI 运行时在本次调用内调度，退出后没有常驻消费器。排队任务与已发送硬件作业分开管理，不能跨电脑插队。

## 设置默认打印机

收到“把 28 楼工作区设为默认打印机”之类的请求时，使用当前技能解释器运行 `scripts/cli.py set-default --printer "28楼工作区"`。该命令只保存默认设备，不创建打印任务；名称不唯一时让用户提供完整名称或设备 ID。网页「打印设备」中的「设为默认」和该命令保存到同一份配置。打开新网页及未指定设备的 WorkBuddy 打印使用该默认值；临时切换与历史任务原目标独立。

右侧快捷表单的份数位于「更多设置」，独立新建打印页直接显示份数和优先级。网页份数为 1–99 的整数，默认 1；原请求重试继续使用原份数。

## 打开网页工作台

WorkBuddy 收到打开网页工作台的请求时，使用本说明开头的平台配置入口并省略 `--first-use`。手动调试可在 Windows 双击 `启动网页控制台.bat`，或使用同一环境前台启动：

```powershell
& $PythonExe "$SkillRoot\scripts\web_server.py" --host 127.0.0.1
```

默认端口来自 `config.json`，交付配置为 18828。自动配置入口检查同目录服务并返回实际本机地址。

只有用户需要手机/同事局域网访问时，才启动根目录 `启动局域网控制台.bat`，或指定 `--host 0.0.0.0`。访问运行主机的真实局域网 IP，**不是打印机 IP**；该服务没有登录鉴权，只在可信公司网络内使用。仅本机监听时，页面显示的局域网链接不能被其他设备访问。

网页通过工作台、打印设备、任务队列、打印记录四个独立视图组织内容，保留设备切换、实时状态、优先级调整/置顶/取消、文件上传和网络诊断。顶部统一选择设备，状态、队列、历史与新任务目标保持一致；网页重新打开时使用技能配置的默认设备。临时切换仅影响当前页面，设备移除时提示重选。设备列表仅在“打印设备”页展示。工作台仅展示前 5 条活动任务，完整任务队列默认每页 5 条，可选 10 / 20 条。本机历史使用后端分页，支持文件名 / 编号、提交人、状态和日期筛选；设备作业单独展示，仅限设备当前可查询日志。可靠关联的本机任务不重复展示；无法可靠关联时保留设备记录。浏览历史发现变化时点击“有新记录”更新，保留页码和详情展开状态。桌面端使用紧凑摘要栏和 176px 侧栏，打印动态并入队列；型号 / IP / 网关、更多操作及普通连接提示按需展开，影响打印的告警直接显示。右侧打印面板随滚动保持可见，文件选择前置，优先级在“更多设置”，目标只读并可聚焦顶部切换；矮窗口表单内部滚动；Mac 分屏和手机窄屏通过“新建打印”进入独立表单视图，切换视图保留填写内容。页面使用线条图标和文字替代表情。默认每 3 秒刷新。网页上传持久保存后返回任务编号，后台转换与投递，任务卡片可核查状态。上传结果不明先按原 request_id 查询，原页面重试沿用同一标识；不要重新发起补打。转换中可取消且不会再发送；发送中暂不能直接取消；结果未知先核查具体硬件作业。

## 网络诊断与初始化

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" diag --printer "27楼"
& $PythonExe "$SkillRoot\scripts\setup.py"
```

诊断覆盖本机 IP、代理、Ping、IPP/RAW/Remote UI 端口和 IPP 握手。按实际诊断结果给建议，不自动修改系统代理、防火墙或网络权限。

`setup.py` 绑定当前解释器并检查设备连通。依赖安装和网页启动使用本说明开头的平台入口。

## 历史研发数据格式说明（原 v2.7.0）

升级前停止本目录所有网页服务和 CLI，备份整个 `data/` 与 `config.json`。首次运行将旧 `data/queue_state.json` 迁移到 `data/queue_state.sqlite3`，原 JSON 保持不变，另保存 `.pre-sqlite.bak`；迁移在事务内进行，失败会报错，不启用空队列。迁移成功后 JSON 不再更新。不要在仍有旧版进程运行时升级。

进程中断遗留的处理任务在下次取得设备锁时标记为“结果未知”，不自动重投。只有硬件完成确认才记完成；设备不返回作业身份或日志已过期时保留未确认状态。未知任务在任务队列中核查，不混入已结束历史。

新增 GET `/api/history`：`printer_id,page,size,search,user,status,start,end`（日期 YYYY-MM-DD），返回 `items,total,pages,page,size`；size 为 5 / 10 / 20。GET `/api/task?job_id=...` 或 `request_id=...` 查询本次提交；`verify=1` 主动核查有硬件编号的待确认任务。POST `/api/upload_print` 保留原表单参数，可加 `request_id`；持久保存后返回 202 和 `job`，不代表已投递或出纸。未提供 request_id 的旧调用兼容，但不提供跨请求防重。

回退旧版本：先停止新版全部进程，保留当前完整目录另作备份，再恢复升级前代码、JSON 及对应文档文件。旧版不能读取 SQLite，不能直接覆盖代码回退；升级后新增记录也不会自动写回旧 JSON。发行包不包含运行中的数据库、上传文件、历史、锁文件或本机备份。恢复数据库备份时保留完整 data 目录，不能只复制正在写入的数据库文件。

## 正式交付版本

当前导入包为 enterprise-printer-hub-0.2.2-import.zip，提供 Windows／Mac 首次配置和本机控制台启动入口。导入包排除历史 EXE、虚拟环境及用户数据。发行包内 manifest 与本文件版本必须一致。

## 网页操作细节（0.0.1）

网页任务卡片的“查看队列 / 查看记录”先核查任务当前阶段，再切换原设备并定位任务；已结束任务进入本机历史，活动任务进入队列。提交前展示目标设备、单双面与优先级摘要，不新增确认弹窗；待核查提交的重试继续显示并沿用原设置。文件“更换 / 移除”只作用于待提交表单，不删除磁盘原件或已提交任务文件；发送或原页面待核查期间禁止改动该次文件快照。

姓名 / 工号首次为空；只有勾选“在本浏览器记住姓名”才写入本浏览器，可用“清除姓名”撤销。不勾选时刷新不保存（待核查提交恢复只恢复该次原始信息）。历史日期填反时提示且不查询；当前筛选条件可见，可一键清除。这些均是前端交互，不改变后端打印与数据规则。
