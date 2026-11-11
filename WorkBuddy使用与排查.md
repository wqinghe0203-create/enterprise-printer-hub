# WorkBuddy 打印技能：使用指令与导入排查

适用技能：enterprise-printer-hub（企业智能文印中心），正式交付版本 0.2.2。本文只整理用法，不会执行打印或修改设备。

## 一、截图中的“安全检测失败”

截图正文为：“检测服务返回异常，未能完成安全检测。你可以跳过检测直接安装，或取消本次安装。”

这表示导入环节的安全检测没有完成，不等于检测发现恶意代码，也不等于已经通过安全检测。仅凭截图不能定位具体失败原因。网络 / 代理连接、客户端状态、检测服务异常，以及特定技能包的扫描处理兼容性都只是排查方向，需要错误日志验证；不能将它与打印机离线、缺少 Python 或打印失败混为一谈。

建议排查顺序：

1. 取消本次导入，重启 WorkBuddy 后重试；检查客户端是否有可用更新。
2. 对比另一个来源可信的小型技能包：若也失败，优先排查客户端、账号会话、网络或检测服务；若只有本包失败，重点检查本次导入包、大小和扫描处理日志。这是定位方法，不是安全结论。
3. 若公司网络有代理 / 安全网关，核实是否阻断检测请求；由管理员依据实际错误确认，不盲目关闭防火墙或安全软件。
4. 重试仍失败，保存失败时间、WorkBuddy 版本、系统版本、导入包名称 / 大小及对应日志，向 WorkBuddy 官方反馈。日志对外提供前先检查是否含令牌、内部地址或文件内容。
5. “跳过检测，直接安装”仅代表跳过扫描，不代表扫描通过；不把它当成修复方案。先核实来源和内容，再自行决定是否安装。

0.0.1 导入包包含 Python、Windows／Mac 配置脚本与网页。首次使用可说“打开打印后台”。安全检测问题仍由 WorkBuddy 客户端处理。

官方参考：

- [技能说明](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)：自行上传的技能通过上传入口导入；安装前应核实来源、权限和脚本内容。
- [常见问题与反馈](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/FAQ)：未解决的问题可向 workbuddy@tencent.com 咨询。本文不代为发送任何信息。

## 二、直接发给 WorkBuddy 的话术

先在 WorkBuddy 已安装技能列表确认该技能可用且已启用。以下使用普通对话，不假定存在 /print、/printer 等斜杠命令。

### 推荐的通用开头

> 请使用 enterprise-printer-hub（企业智能文印中心）技能，先读取它实际安装目录中的 SKILL.md，再按现有 CLI 完成下面的任务。指定设备时先核实配置；没有匹配就告诉我，不要改用默认打印机。

### 常用指令

| 目的 | 可以直接复制的对话 |
| --- | --- |
| 列出设备 | 使用 enterprise-printer-hub 技能，查看已配置的打印机、位置、IP 和在线状态。 |
| 查询 HP 状态 | 使用 enterprise-printer-hub 技能，查询 NPIC9D84C（28楼工作区）的状态，只查询，不打印。 |
| 查看谁在打印 | 使用 enterprise-printer-hub 技能，看看 NPIC9D84C 现在谁在打印，以及待处理队列。 |
| 单面打印 | 使用 enterprise-printer-hub 技能，把本次附件打印到 NPIC9D84C（28楼工作区），单面 1 份，提交人张三。 |
| 使用设备默认 | 使用 enterprise-printer-hub 技能，把本次附件打印到 NPIC9D84C，使用打印机默认单双面设置，1 份，提交人张三。 |
| 长边双面 | 使用 enterprise-printer-hub 技能，把本次附件打印到佳能 C3130L 总部复合机，长边双面 1 份，提交人张三；如果不支持就停止并说明原因。 |
| 多份打印 | 使用 enterprise-printer-hub 技能，把本次附件打印到 NPIC9D84C，单面 2 份，普通优先级，提交人张三。 |
| 紧急任务 | 使用 enterprise-printer-hub 技能，把本次附件打印到 NPIC9D84C，单面 1 份，优先级 100，提交人张三。 |
| 已排队任务置顶 | 使用 enterprise-printer-hub 技能，把本机尚未发送的任务 #101 置顶。 |
| 调整优先级 | 使用 enterprise-printer-hub 技能，将本机排队任务 #101 的优先级设为 80。 |
| 取消本机任务 | 使用 enterprise-printer-hub 技能，取消本机任务 #101；如果已经开始发送，先核查状态，不要直接宣称取消成功。 |
| 取消硬件任务 | 使用 enterprise-printer-hub 技能，先核对 NPIC9D84C 的硬件队列，再取消该设备硬件任务 #17；不要把它当作本机任务 #17。 |
| 核查未知结果 | 使用 enterprise-printer-hub 技能，核查本机任务 #104 和目标设备队列，不要重新提交文件。 |
| 查看历史 | 使用 enterprise-printer-hub 技能，打开网页控制台的打印记录，筛选 NPIC9D84C 的本机记录。 |
| 打开本机控制台 | 使用 enterprise-printer-hub 技能，打开本机网页控制台；已有服务就复用，启动后确认 HTTP 可访问并给我地址。 |
| 手机访问 | 使用 enterprise-printer-hub 技能，为手机开启可信公司局域网访问，给我运行服务电脑的局域网地址，不是打印机地址。 |
| 只检测新设备 | 使用 enterprise-printer-hub 技能，检测 10.1.36.229 能否接入，只检测，不保存，不打印测试页。 |
| 新增设备 | 使用 enterprise-printer-hub 技能，添加网络打印机 10.1.36.229，名称“研发区打印机”，位置“26F 研发区”，检测兼容后保存，不打印测试页。 |
| 网络诊断 | 使用 enterprise-printer-hub 技能，诊断 NPIC9D84C 的网络连接，保留真实错误，不修改代理或防火墙。 |

以上 #101、#104、硬件 #17 均为示例，先查询实际任务再替换；文件必须是本次真实附件或存在的本地路径。示例设备来自当前项目配置，其他电脑安装后的配置可能不同。

## 三、实际 CLI（PowerShell）

以下仅供人工核查或让 WorkBuddy 按技能执行；打印、取消、新增命令会产生实际效果，不要把整节当成测试脚本批量运行。

### 1. 一次性设置路径与解释器

```powershell
# 以 WorkBuddy 实际加载的技能目录为准；下方是常用安装目录。
$SkillRoot = Join-Path $env:USERPROFILE '.workbuddy\skills\enterprise-printer-hub'
if (-not (Test-Path (Join-Path $SkillRoot 'SKILL.md'))) { throw '技能目录不存在，请改为实际安装目录' }
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
if (-not $PythonExe -or -not (Test-Path $PythonExe)) { throw '请先运行安装依赖.bat，或安装 Python 3.9+' }
& $PythonExe -c 'import sys; assert sys.version_info >= (3, 9); print(sys.executable)'

# 只读取本机配置，以实际打印机 ID 为准。
(Get-Content (Join-Path $SkillRoot 'config.json') -Raw | ConvertFrom-Json).printers |
  Select-Object id, name, location, ip
$PrinterId = 'printer_796e90e5ffc74775944350471c25b9d8' # 当前项目的 NPIC9D84C 示例 ID
```

安装依赖使用技能根目录的 `安装依赖.bat`；Word / Excel 需由管理员配置内网转换服务。可在本机控制台点击「重新检测」，或通过技能 Python 运行 `scripts/bootstrap.py --retry-office`。服务不可用时可改传 PDF。不要使用别台电脑遗留的解释器路径。

### 2. 查询与诊断

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" --help
& $PythonExe "$SkillRoot\scripts\cli.py" printers
& $PythonExe "$SkillRoot\scripts\cli.py" status --printer $PrinterId
& $PythonExe "$SkillRoot\scripts\cli.py" queue --printer $PrinterId
& $PythonExe "$SkillRoot\scripts\cli.py" diag --printer $PrinterId
```

### 3. 打印（路径需替换为真实文件）

```powershell
# 单面一份
& $PythonExe "$SkillRoot\scripts\cli.py" print --file 'C:\实际目录\文件.pdf' --printer $PrinterId --user '张三' --duplex simplex --copies 1 --priority 50

# 按设备默认单双面设置，两份
& $PythonExe "$SkillRoot\scripts\cli.py" print --file 'C:\实际目录\文件.pdf' --printer $PrinterId --user '张三' --duplex auto --copies 2 --priority 50
```

`--duplex`：`auto` 设备默认、`simplex` 单面、`duplex_long` 长边双面、`duplex_short` 短边双面。不传时 CLI 仍默认长边双面；HP 是否支持双面以设备能力和 Validate-Job 为准，不静默降级。普通优先级 50，高优先级 80，加急 100；仅调整本机待发送顺序，不抢占已接收的硬件作业。

### 4. 任务调整与取消（编号需替换）

```powershell
& $PythonExe "$SkillRoot\scripts\cli.py" cut-in --job-id 101
& $PythonExe "$SkillRoot\scripts\cli.py" priority --job-id 101 --priority 80
& $PythonExe "$SkillRoot\scripts\cli.py" cancel --job-id 101
& $PythonExe "$SkillRoot\scripts\cli.py" cancel --job-id 17 --printer $PrinterId --hw
```

本机编号和硬件编号不同；发送结果不明时先核查，不自动补打。各自安装的技能拥有各自的本机记录。

### 5. 检测与添加打印机

```powershell
# 只检测
& $PythonExe "$SkillRoot\scripts\cli.py" add-printer --ip '10.1.36.229'
# 明确确认新增后再执行
& $PythonExe "$SkillRoot\scripts\cli.py" add-printer --ip '10.1.36.229' --name '研发区打印机' --location '26F 研发区' --save
```

### 6. 网页入口

```powershell
# 本机控制台；前台运行，保持窗口开启。
& $PythonExe "$SkillRoot\scripts\web_server.py" --host 127.0.0.1
# 仅在明确需要手机 / 同事局域网访问时，改用下面的命令。
# & $PythonExe "$SkillRoot\scripts\web_server.py" --host 0.0.0.0
```

先确认端口是否已有服务，不重复启动。交付配置默认端口 18828，本机地址为 http://127.0.0.1:18828，记录视图为 http://127.0.0.1:18828/#history。启动后用真实 HTTP 响应确认可访问；局域网 URL 使用服务电脑的 IP，服务没有登录鉴权。

当前没有 `cli.py history` 或 `cli.py web` 子命令：历史使用网页，网页由 `scripts/web_server.py` 启动，不编造 CLI 命令。

## 四、普通员工看到的打印结果

打印后默认展示一句中文结果和五项表格（打印文件、打印机、打印设置、提交人、任务编号）。页数、份数和单双面合并为“打印设置”。只报告已观察到的真实阶段：已提交不等于设备确认完成；未确认时不会直接说完成，结果未知时提醒不要重复提交。信息缺失就省略，不猜测。

普通成功反馈不附英文状态码、文件协议、硬件编号、内部路径或长篇注意事项。失败时简要说明原因，必要时只补充一句操作建议。需要技术详情时可另问“请解释这次打印的技术详情”。此规范不改变打印功能和数据存储。

0.0.1 起，明确指定设备但未找到或匹配到多台时，程序停止并提示选择，不再回退默认打印机。未指定设备仍使用有效的配置默认设备；配置损坏或默认设备不存在时先修复配置。准确设备 ID 优先于完整名称，完整名称优先于模糊关键词。

网页 0.0.1 提供文件更换 / 移除、提交前设置摘要、任务原设备定位与筛选条件清除。首次使用需填写姓名 / 工号，勾选后才会在本浏览器保存；公共电脑可保持不勾选，并通过“清除姓名”移除已记住的信息。移除待选文件不会删除磁盘原件或已提交任务。
