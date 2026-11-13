# 企业智能文印中心 · WorkBuddy 技能包

版本：0.1.5。支持 WorkBuddy 对话调用与网页控制台，包含多设备状态、实时作业、队列优先级管理、文件投递、历史记录和网络诊断。

## 中心服务器部署（Linux / Windows + Docker Gotenberg）

适用于公司在内网搭建**唯一中心打印服务器**，同事通过 WorkBuddy 远程瘦客户端（`scripts/remote_cli.py`）或浏览器共用同一打印队列与历史，无需在每台电脑安装 Office / LibreOffice 或 Python 第三方库。

### 1. 启动 Docker Gotenberg 文档转换服务（默认 `http://127.0.0.1:3000`）

```sh
cd deploy
docker compose -f docker-compose.gotenberg.yml up -d
# 验证转换引擎在线：
curl http://127.0.0.1:3000/health
```

> 若服务器需打印中文 Word / Excel / PPT / Markdown，建议取消 `deploy/docker-compose.gotenberg.yml` 中字体目录挂载的注释，将宿主机中文字体（如思源黑体 / Windows Fonts）只读挂载到容器内 `/usr/share/fonts/custom`。

### 2. 启动中心打印服务（Linux / Windows）

* **Linux 服务器**：
  ```sh
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  cp deploy/linux/enterprise-printer-hub.env.example /etc/enterprise-printer-hub.env
  # 按需配置 PRINT_HUB_GOTENBERG_URL、PRINT_HUB_API_TOKEN、PRINT_HUB_ADMIN_TOKEN
  .venv/bin/python scripts/web_server.py --host 0.0.0.0
  ```
  *(生产托管可将 `deploy/linux/enterprise-printer-hub.service` 注册为 systemd 常驻服务。)*
* **Windows 服务器**：先运行根目录 `安装依赖.bat`，再双击 `deploy/windows/启动中心打印服务.bat`。

### 3. 环境变量与可选安全口令

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PRINT_HUB_CONVERTER_MODE` | `gotenberg` | `gotenberg`（失败直接报错不出纸）/ `local`（本机 COM 或 LibreOffice）/ `auto` |
| `PRINT_HUB_GOTENBERG_URL` | `http://127.0.0.1:3000` | Gotenberg 服务地址（可指向另一台 Docker 主机） |
| `PRINT_HUB_GOTENBERG_USER` / `_PASSWORD` | 空 | 跨机部署启用 Gotenberg Basic Auth 时填写 |
| `PRINT_HUB_API_TOKEN` | 空 | 可选访问口令；设置后非本机调用上传/取消/优先级须携带 `X-Print-Hub-Token` |
| `PRINT_HUB_ADMIN_TOKEN` | 空 | 可选管理口令；设置后允许远程添加打印机与修改默认打印机 |
| `PRINT_HUB_REGISTRATION_ORIGIN` | 空 | 中心后台允许普通访问者添加打印机的网页来源，例如 `https://10.0.249.200:8086`，填写浏览器地址的协议、主机和端口 |

### 4. 同事侧 WorkBuddy 接入配置

员工使用 `enterprise-print` 时，首次在同一张卡中选择默认打印机、单双面、优先级和提交人。可以直接打印 WorkBuddy 生成的 DOCX。技能保存设置后会打开中心后台，系统默认浏览器中同一后台的标签页会同步并回显个人设置。

同事只需导入精简技能包，并在技能根目录将 `remote.example.json` 复制改名为 `remote.json`（填入中心服务器地址），或设置环境变量 `PRINT_HUB_SERVER=http://服务器IP:18828`（如开启了口令则再配置 `PRINT_HUB_API_TOKEN`），即可直接用对话打印 PDF、Word、Excel、PPT、TXT、Markdown、HTML 与图片，**零本地依赖**。

---

## WorkBuddy 首次使用（本机独立运行模式）

### Windows 首次配置

再次打开管理台时优先复用正在运行的后台。首次缺依赖时，管理员可在 PowerShell 中按需设置 `PRINT_HUB_PIP_INDEX_URL` 或 `PRINT_HUB_WHEEL_DIR`，再运行 `powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1 --install-only`。若公司允许直连且已确认官方源可用，可将源指定为 `https://pypi.org/simple/`。离线目录需要适配 Windows、当前 Python 及 CPU 架构，并包含 `pypdf`、`Pillow`、`PyMuPDF`、`pywin32` 及其依赖。设置离线目录后会优先从本地安装。

### Mac 首次配置

Apple 芯片 Mac 先把 `LibreOffice_26.8.2_MacOS_aarch64.dmg` 中的应用安装到「应用程序」。导入技能后说「启动打印后台」。Word 和 Excel 会分别显示转换检查结果。手动安装后，在原页面点击「重新检测」即可保留已选文件和打印设置。

技能先复用已配置的 Python 依赖。公司网络限制下载时，管理员可在实际安装目录一次性运行以下任一命令。离线目录须位于技能目录之外，包含匹配 Mac 架构和 Python 版本的 `pypdf`、`Pillow`、`PyMuPDF` 及其所需 wheel。

```sh
PRINT_HUB_WHEEL_DIR="/path/to/mac-wheels" /bin/zsh ./scripts/bootstrap-mac.sh --install-only
PRINT_HUB_PIP_INDEX_URL="https://pypi.example.internal/simple/" /bin/zsh ./scripts/bootstrap-mac.sh --install-only
```

两者同时设置时优先使用离线目录。未设置时沿用现有 pip 配置。配置完成后，WorkBuddy 会复用同一安装目录的 `.venv`。

导入后说“打开打印后台”。技能会在实际安装目录配置 `.venv`，然后打开本机网页。Apple 芯片 Mac 手动安装 LibreOffice 26.8.2。管理台在后台分别验证 Word 和 Excel 转换。需要手动运行时，在技能目录执行对应命令：

```powershell
powershell.exe -NoProfile -File .\scripts\bootstrap-windows.ps1
```

```sh
/bin/zsh ./scripts/bootstrap-mac.sh
```

自动配置可使用已有 winget 或 Homebrew。遇到系统安装确认时按系统提示操作。Mac 的 Word／Excel 直传需要 LibreOffice，Windows 需要 Office／WPS 的转换组件。安装暂未完成时可先导出 PDF。Mac 可用 `--office-status` 查询当前结果。后台启动时按需检查一次，网页轮询和提交只读状态。需要复检或仅用 CLI 且尚无检查结果时，先运行 `/bin/zsh ./scripts/bootstrap-mac.sh --retry-office`，对应格式通过后再提交。用 `--no-browser` 只启动服务，用 `--install-only` 只配置环境。端口冲突时可加 `--port 18829`。

在「打印设备」点击「设为默认」，或在 WorkBuddy 说“把 28 楼工作区设为默认打印机”，即可为当前技能安装保存默认设备。下次打开网页或未指定设备打印时使用该默认值。右侧快捷打印可在「更多设置」填写份数，独立新建打印页直接显示，默认 1 份。

## Windows 同事安装

1. **完整解压 ZIP**，不要在压缩包预览窗口里直接运行。将里面的 `enterprise-printer-hub` 文件夹放到 `%USERPROFILE%\.workbuddy\skills\` 下。最终路径应是 `%USERPROFILE%\.workbuddy\skills\enterprise-printer-hub\SKILL.md`，避免重复套两层同名文件夹。若 WorkBuddy 使用自定义技能目录，以实际目录为准。
2. 按上面的 WorkBuddy 首次使用流程配置。手动安装时可用 Python 3.9+，双击 `安装依赖.bat` 创建技能自己的 `.venv`。首次下载依赖需要联网。
3. 刷新 WorkBuddy 的技能列表或重新打开 WorkBuddy，确认发现 `enterprise-printer-hub`。在对话中说“查看公司打印机列表”“现在谁在打印？”即可调用。
4. 打印 Word / Excel 还需要这台服务电脑安装可用的 Microsoft Word / Excel 或支持 COM 的 WPS；仅安装 pywin32 不包含 Office。PDF 不需要 Office，图片转换使用 Pillow。
5. 连接可访问打印机的公司网络。包内 `config.json` 保留 27F 总部佳能 `10.1.36.228` 的配置；异地或不同网络需要修改设备 IP / 配置。

这是需要 Python 的源码技能包，不是免安装 EXE。首次调用按需配置 Python 环境，打印使用现有 IPP 流程。

## 网页控制台

双击 `启动网页控制台.bat`，看到启动日志后用浏览器打开 `http://127.0.0.1:18828`。保持命令窗口开启，Ctrl+C 停止服务。脚本默认仅允许本机访问，端口以配置/启动日志为准。

需要手机或其他同事通过局域网访问时，在可信公司网络内双击 `启动局域网控制台.bat`，使用日志中的 LAN URL。它没有登录鉴权，不应映射到公网；首次跨设备访问可能需要由用户允许 Windows 防火墙的专用网络访问。

网页和 CLI 不必同时启动；对话调用 CLI 可独立使用。同一安装目录共用队列；不同同事各自安装后的虚拟队列相互独立。若希望共用一个队列，让同事访问同一台已启动的网页服务。

## 添加网络打印机

中心后台配置 `PRINT_HUB_REGISTRATION_ORIGIN=https://10.0.249.200:8086` 后，访问该地址的用户可在「打印设备」点击「添加打印机」。本机部署也可从服务电脑打开 `http://127.0.0.1:18828/#devices`。

手动用 `nohup` 启动时，在原启动命令前加 `PRINT_HUB_REGISTRATION_ORIGIN=https://10.0.249.200:8086` 并重启 Python 服务。刷新后若按钮仍为灰色，检查中心后台 `/api/dashboard` 返回的 `printer_registration_origin` 是否与浏览器地址的来源相同。

1. 输入公司私网 IPv4 地址；高级设置可修改 IPP 端口（默认 631）和路径（默认 /ipp/print）。
2. 点击“检测设备”。只读取型号、文档格式、色彩、单双面和份数能力，不打印测试页；后台检测单次连接超时 6 秒，不占用网页主请求处理。
3. 显示“可以接入”后，填写名称与位置，点击“确认保存”。新设备立即出现在设备列表和上传目标中，不重启、不改变默认设备或现有表单。

添加功能支持与现有 PDF / PWG Raster 链路兼容的网络设备。连接失败、不兼容或重复的设备不会保存。接口接受配置的中心网页来源、本机同源访问及现有管理员令牌。全局默认设备仍由管理接口单独设置。

WorkBuddy 可运行 `scripts/cli.py add-printer --ip 10.1.36.229` 检测；明确新增时加 `--save --name "研发区打印机" --location "26F"`，支持 `--ipp-port`、`--ipp-path`。检测与保存共用同一逻辑，禁止默认测试出纸。

保存会锁定配置文件，先备份 `config.json.bak`，再通过同目录临时文件原子替换；配置损坏、重名 ID、重复地址或写入失败不会覆盖原设备。恢复时先停止服务，保留当前配置副本，再用已核对的备份恢复。

**升级保留数据**：覆盖更新代码前备份原目录，保留原有 `config.json`、`data/` 和机器自身环境；不要把发行包的初始配置、空队列覆盖到正在使用的安装目录。

## 网页布局与分页

工作台、打印设备、任务队列、打印记录使用独立视图，可通过侧栏（手机顶部导航）切换，支持浏览器前进/后退。工作台只展示当前设备前 5 条活动任务，点击“查看全部”进入任务队列；任务队列默认每页 5 条，可选 10 或 20 条。本机打印记录支持完整后端分页及文件名 / 编号、姓名、状态、日期筛选；设备作业单独展示，仅包含设备当前能提供的日志。可靠关联的任务只展示一次。自动刷新保留当前页和展开的详情，历史变动先提示“有新记录”，点击后更新。长失败说明折叠展示，避免撑高表格。

桌面端右侧“新建打印任务”在滚动时保持可见，矮窗口内表单独立滚动。Mac 分屏与手机窄屏点击“新建打印”进入独立表单视图；视图切换保留已选文件和填写信息。使用线条图标及文字状态，减少表情干扰。

## WorkBuddy 调用示例

- “查看公司打印机列表”“现在谁在打印？”
- “查看总部打印机排队情况”
- 上传文件后说“把这份 PDF 打到总部”
- “把任务 #101 设为紧急插队”“将任务 #101 优先级调为 80”
- “取消任务 #101”“诊断总部打印机网络”“打开打印后台”

具体的参数、设备定位、硬件/虚拟任务区别与结果判断规则见 [SKILL.md](SKILL.md)。

## 配置与手动运行

修改 `config.json` 的 `printers` 数组可添加设备；`system.default_printer_id` 指定默认设备。设备需要实际支持所配置的 IPP 地址与端口。

Windows 在项目目录执行：

```powershell
& .\.venv\Scripts\python.exe scripts\cli.py --help
& .\.venv\Scripts\python.exe scripts\cli.py printers
& .\.venv\Scripts\python.exe scripts\cli.py queue --printer canon_c3130l_27f
& .\.venv\Scripts\python.exe scripts\cli.py diag --printer canon_c3130l_27f
& .\.venv\Scripts\python.exe scripts\web_server.py --host 127.0.0.1
```

可选运行 `scripts/setup.py` 记录当前解释器并检测设备连接；该脚本不安装 pip 依赖、Office 或打印驱动。不要沿用其他电脑生成的 `_local.json`。

## macOS / Linux

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/web_server.py --host 127.0.0.1
```

Office 转 PDF 使用 LibreOffice：从 PATH 查找 `soffice` / `libreoffice`，也支持 Mac 的 `/Applications/LibreOffice.app` 与用户 Applications 目录。每次转换使用独立配置和临时目录，90 秒超时，并校验生成的 PDF。Mac 将转换临时文件放在本机 `/private/tmp`，校验通过后在输出目录内发布 PDF，技能目录或原文件可位于外接盘。Mac 转换子进程使用独立 Fontconfig 配置读取当前系统字体及 AssetsV2 中文字体，不修改系统字体设置；检查 PDF 中可见中文的缺失字形，发现缺字会阻止投递并提示重新导出。缺少转换器时可以先导出 PDF。Mac 页面支持系统字体、Finder 拖放、分屏窗口和窄屏卡片布局。

## 当前功能边界

- 优先级针对本机虚拟队列，不抢占已发送的硬件作业；网页运行时持续后台消费，同一设备串行转换与发送；CLI 仅在调用期间调度，不安装后台系统服务。
- 双面和份数通过 IPP 作业属性发送，并由 Validate-Job 预检。设备在双面预检中拒绝 `sides` 时，后台用设备报告的默认纸张再预检一次。通过后按相同参数投递。PWG Raster 支持 8 位 sRGB / sGray、72–600 dpi 的方形分辨率及 `normal` 双面背页变换。
- 自动协商 PDF 或 PWG Raster。设备未声明 PDF 支持时，先用 PyMuPDF 光栅化，不再以 application/octet-stream 发送 PDF。
- 作业接收后为“已提交”，硬件 job-state=9 才记为完成；终止和取消同步回本地。超时 / 缺失响应显示“发送结果未知”，禁止自动重投；旧版本无硬件确认的“已完成”记录显示“出纸未核实”。
- 页面显示具体硬件作业失败原因，以及缺纸 / 墨粉等设备告警。单纯的马达响应或接收提示不能证明纸张输出。
- 未指定设备时使用配置默认设备；明确指定设备但不存在或有多个匹配时停止，并提示重新选择。默认设备失效或配置损坏时停止，不改投第一台设备或内置配置。

## 交付与验证

ZIP 保留代码、UI、技能入口、配置、Windows／Mac 脚本和依赖声明，首次运行初始化本机 SQLite 队列（旧 JSON 存在时先迁移）；不包含本机上传文件、历史任务、虚拟环境、缓存、开发记忆或机器绑定路径。

已验证 Office 转换、状态回归和多页彩色 / 灰度 PWG Raster；输出使用 macOS 原生 CUPS 解码器独立校验。针对总部设备执行了不创建打印作业的 Validate-Job，确认新格式和参数通过预检。Windows COM、BAT 执行及 WorkBuddy 自动发现需在同事的 Windows 环境验收；尚未重新执行实际出纸测试。

PWG Raster 格式实现依据 [CUPS Raster 规范](https://openprinting.github.io/cups/libcups/spec-raster.html)，PDF 页面渲染使用 [PyMuPDF get_pixmap](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_pixmap)。

### 历史研发记录 v2.6.0 验证范围

原有 30 项回归加新增配置、来源限制、异步检测和重复保存测试共 56 项通过。隔离浏览器验收覆盖检测 / 保存 / 立即刷新、保留原选择与表单、重复设备、连接失败、Esc 和 390px 全屏弹窗；正式配置和队列保持原样。Windows 文件锁、BAT、Office COM、Chrome/Edge、WorkBuddy 发现，以及 Safari 实机验收尚未执行。未自动添加真实设备或发送打印任务。

### 历史研发记录 v2.6.1 HP 兼容修复

网页新增单双面选项，默认读取打印机的实时默认设置；缺失时使用单面。CLI 可传 `--duplex auto`，不传仍保留长边双面。旧网页/API 未提供 duplex 时保留配置中的默认值。

解析 IPP 不支持属性组及带语言的名称，明确提示被拒绝的单双面、份数或格式，保留 Validate-Job 严格预检，不能自动降级或重投。修正空位置括号、缺失网关及有线设备的无线未配置提示。

60 项自动测试通过。真实 HP 仅做读取与 Validate-Job：PDF 单面通过，长边双面被标记为不支持的设置；未提交实际打印。Windows / Safari 实机和实际出纸尚未验收。

## 历史研发记录 v2.7.0 本机一致性与提交反馈

- 每位同事独立安装；同一目录的网页和 CLI 使用 `data/queue_state.sqlite3` 共用事务记录与设备发送锁。不同电脑不共享历史，也不协调本地优先级。数据库必须放本地磁盘，不能放网络共享目录。
- 网页文件上传并保存任务后立即返回编号，后台转换、校验与发送；提交卡片持续显示阶段、目标设备和失败原因，支持核查状态。重复请求使用同一个提交标识，只创建一个任务。
- 发送超时或进程中断后不自动补打；未取得硬件编号的发送中任务不能直接标为取消。新建任务仍保留原有打印格式协商、单双面、份数与硬件完成确认规则。
- 本机历史与设备作业分别查看；设备离线不影响本机历史。文档页数、份数与设备报告完成张数分别展示，缺失数据标为未知。

### 升级与回退

1. 停止本安装目录全部网页和 CLI 进程，备份 `config.json` 及整个 `data/`，再覆盖代码。不要覆盖或清空已有数据目录。
2. 首次启动事务迁移旧 `queue_state.json`，保留原文件，并备份为 `queue_state.json.pre-sqlite.bak`；保留编号、设置及硬件关联。迁移失败停止并提示，不自动创建空队列替换旧记录。迁移成功后 JSON 不再更新。
3. 恢复数据库使用停机后的完整 data 备份；不要复制正在使用的单个数据库文件。SQLite 损坏时停止服务、备份现场再人工恢复，不能删除数据库并把过期 JSON 当作最新数据。
4. 回退旧代码需停机并恢复升级前完整备份；旧版不识别 SQLite，新版新增任务不会自动回写 JSON。请保留新版完整副本供核查，避免重复投递仍在打印机上的任务。

### 本次验证方式

运行 `python -m unittest discover -s tests -q`；并发与中断测试使用临时数据库和模拟传输，不访问真实打印机。可选浏览器回归见 `tests/browser_smoke.py`（需安装开发依赖 playwright，使用本机 Edge）；运行时采用隔离 HTTP 服务与模拟设备，不启动真实消费器。真实 Office / WPS 转换、WorkBuddy 调用及实际出纸需在目标环境人工验收。

2026-11-02 本次 Windows 验证：75 项测试中 73 项通过、2 项 macOS 专用集成测试跳过；本机 Edge 无头浏览器回归通过，覆盖历史分页筛选、阅读位置保留、响应丢失后防重、刷新页面后的原请求恢复与 390px 窄屏。跨进程队列写入、Windows 文件锁、单次投递及进程中断恢复使用隔离数据验证。未发送真实打印任务。

## 历史研发记录 v2.7.1 紧凑工作台

顶部设备选择器统一控制当前状态、任务队列、打印记录与新任务目标，设备列表的“使用设备”也走同一切换流程。浏览器重新打开时选择本安装的默认设备。临时切换仅影响当前页面；设备移除时必须重新选择。切换或加载失败时清空旧设备内容、禁用新的提交，防止看到的设备与打印目标不一致；正在核查或重试的提交始终沿用原设备与设置。

工作台移除设备大卡片，三张统计卡合并成摘要栏。设备详情、更多操作及不影响连接的提示按需展开；离线和缺纸等告警继续直接展示。设备页使用紧凑列表。当前打印动态并入任务队列，无任务时只占一行。

桌面左栏收窄到 176px，右侧随桌面窗口自适应至 340–400px（1440px 窗口为 360px）的打印面板保留固定定位，文件选择前置，优先级移入“更多设置”。右侧目标只读，点击“切换”聚焦顶部选择器；切换不清空文件、姓名、单双面和优先级。提交卡片保留原目标设备，详细结果按需展开。手机继续通过“新建打印”使用独立表单。

开发验证：`python tests/browser_workspace.py`（需 playwright 和本机 Edge）。使用静态页面与模拟 API，覆盖多设备快速切换、迟到响应、文件 / 设置保留、提交目标、已提交任务目标保持、上次设备恢复、设备移除、离线、加载失败、普通 / 缺纸告警、前 5 条 / 完整分页，以及 1440×900 首屏和 390 / 768 / 1024px 布局。`tests/browser_smoke.py` 原有历史筛选、防重恢复回归也通过；75 项单元测试中 73 项通过，2 项 macOS 专用测试跳过。此次不改变后端接口、存储与投递逻辑，不发送真实打印任务。

## 正式导入包

使用 `python tools/build_skill_package.py` 构建 `dist/enterprise-printer-hub-0.1.5-import.zip`。构建包含运行必需文件与说明，排除历史 EXE、`.venv`、`.git`、缓存、测试和用户数据。用户导入后，首次调用在本机配置依赖。

已有用户务必先将配置和整个 data 目录备份到技能目录以外，避免客户端同名导入覆盖旧数据。具体步骤见 [导入安装说明](导入安装说明.md)。干净包仅修正交付冗余，不等于已经通过 WorkBuddy 安全检测；若检测服务仍返回异常，需由客户端 / 服务侧继续排查。本次未修改运行功能或安全检测设置。

## 打印后的对话反馈

默认使用“一句中文结果 + 五项信息表格”：打印文件、打印机（含位置）、打印设置（页数 / 份数 / 单双面）、提交人、任务编号。状态使用“设备已确认完成”“已提交，等待设备确认”“已排队”等中文，不同时展示几个阶段。普通成功回复到表格结束；失败或结果未知时最多再给一句必要建议。信息缺失时省略，不猜测。

技术字段、转换产物路径与数据库目录不在普通反馈中展开，用户追问或诊断时再按需说明。具体模板与状态规则见 SKILL.md。此调整只涉及回复规范，不改变打印程序、状态判定、文件保留或数据目录；不自动清理转换文件或合并不同安装副本。

## 历史交付记录 0.0.1

从本次开始采用 0.0.1、0.0.1 等递增交付编号。原 2.x 为历史研发编号；此编号调整不重置数据库或任务编号，不迁移用户数据，不代表打印功能回退。0.0.1 补齐安装检查中的 pymupdf 导入、阻止设备不匹配时误投默认设备，并保留原转换 / 校验 / 格式协商 / 单双面 / 份数 / 调度 / 投递 / 完成确认流程。

使用未指定设备的旧命令仍会选中有效的默认打印机；指定关键词遇到多个候选时，改用完整名称或准确设备 ID。配置文件损坏或默认设备不存在时先修复配置，再操作，不使用内置地址替代。

正式发布时统一更新 SKILL.md、manifest.json、README 当前版本和导入说明，再运行构建工具。构建会检查版本一致性并生成新 ZIP 与 SHA-256 清单；旧压缩包保留，新包不包含旧包和运行数据。导入已有安装目录前仍需停机并备份配置和整个 data，不自动覆盖正在运行的副本。

0.0.1 验证结果：87 项自动测试中 85 项通过、2 项 macOS 专用集成测试跳过；两组隔离浏览器回归通过。对比确认 10 个打印主流程方法及转换、IPP、队列等受保护文件未改变，8 组渲染像素与 8 份 PWG 数据一致。未执行真实打印、安装依赖、覆盖现有安装或切换服务。

## 正式交付 0.0.1：轻量操作细节

- 任务结果中的入口切换到该任务原设备并定位；结束任务查历史，活动任务查队列。原设备已被移除时提示核查，不改投其他设备。查看记录不会提交打印，草稿文件与打印设置仍保留。
- 提交按钮上方显示设备、单双面与优先级摘要；重试显示原提交设置，不增加确认弹窗。
- 选中文件后提供“更换文件 / 移除文件”，仅调整表单选择，不删除原件；发送或保留原请求快照时锁定文件操作，刷新后仍能重新选择原文件继续核查 / 重试。
- 姓名 / 工号初始为空；可主动勾选本浏览器记忆，可随时清除。没有勾选时刷新不保留姓名；公共电脑建议保持不勾选。
- 日期范围不合理时直接提示且不发出新查询；应用的筛选条件可见，支持一键清除。

本版不引入新框架、依赖、接口或服务，不改动后端脚本、配置、数据库或打印参数。已有的格式转换、投递、防重与完成确认规则保持。当前已安装副本不自动覆盖，升级前仍需按导入说明备份。

0.0.1 验证：87 项自动测试中 85 项通过、2 项 macOS 专用测试跳过；两组隔离浏览器回归覆盖五项交互、跨设备任务定位、原请求重试、姓名自愿记忆和窄屏布局。后端脚本、启动器、依赖、配置及 EXE 共 18 个受保护文件哈希未变；现有安装副本未覆盖，未发送真实打印任务。
