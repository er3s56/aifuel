# aifuel

AI 额度悬浮窗。同时盯 Claude 和 ChatGPT/Codex 的用量，置顶小窗，不占地方。

```
CL   5h    27%  ▬▬▭▭▭▭▭▭   19:19
     wk    13%  ▬▭▭▭▭▭▭▭   09-15 01:59
     Fable  7%  ▬▭▭▭▭▭▭▭   09-15 02:00
GPT  5h    --↺  ▭▭▭▭▭▭▭▭
     wk     2%  ▭▭▭▭▭▭▭▭   09-15 13:07
```

**行数是动态的**：服务端返回几条限额就显示几行。Claude 的模型专属周额度
（如 Fable）、ChatGPT 的付费余额，都会在存在时自动出现，不需要改代码——
Claude 的额度类型不做白名单；GPT 显示 Codex 主额度及启用的付费余额。

## 用哪个

| 方式 | 需要什么 | 说明 |
|---|---|---|
| **`AI-Fuel-版本-windows-x64-setup.exe`** | Windows x64；无需 Python | 安装向导，开始菜单入口、可选桌面快捷方式，支持系统卸载 |
| **`dist\aifuel\aifuel.exe`** | 无需 Python；登录要求见下方 | 独立打包，双击即用。整个 `dist\aifuel` 文件夹一起拷走 |
| `run_qt.vbs` | Python + PySide6 | 开发时用，改完代码直接跑 |
| `run_tk.vbs` | Python（tkinter 自带） | 零依赖备选，外观朴素些 |

> **前提**：仅需安装、登录要查询的对应客户端，两家互不依赖。
> Claude 可使用 Windows Claude 桌面端的 Code 登录，或独立 Claude Code CLI 登录；
> GPT 可使用 Windows 桌面端或独立 Codex CLI。
> 只装桌面端时，请先打开对应的 Code / Codex 模式并登录。
> 默认查找 PATH、独立 CLI 安装目录和桌面端组件缓存；缺少组件时显示“组件未就绪”，
> 右键“额度详情”可查看处理方法。非标准安装可用 `AIFUEL_CODEX_EXE` 或
> `AIFUEL_CLAUDE_EXE` 指定对应的命令行组件路径（不是桌面端主程序）。
> 安装包不包含账号登录；仅登录网页不能为 AI Fuel 提供本地查询组件。

安装后自动启动时，Windows 的目录链接保护可能让 Codex 启动路径报 448。
AI Fuel 会读取启动链接并查找当前版本的实际程序文件，保持系统保护开启，
无需重新登录或手工配置路径。

> **杀软**：未签名的 PyInstaller 产物可能被 Defender / SmartScreen 拦，
> 需要手动点“仍要运行”。根治要代码签名证书。

## 操作

| 动作 | 效果 |
|---|---|
| 左键拖动 | 移动窗口（位置自动记住） |
| 双击 | 立即刷新 |
| 右键 | 菜单：显示或隐藏 / 锁定位置 / 刷新 / 额度详情 / 不透明度 / 开机自启 / 退出（前两项仅 Qt 版） |

**Qt / exe 版**提供托盘图标：单击可隐藏或恢复完整悬浮窗，右键可打开相同菜单。
菜单中的“锁定位置”可防止误拖动；再次启动程序也会恢复已隐藏的窗口。
每次启动默认显示悬浮窗，不保存隐藏状态。拔掉副屏或工作区改变后，窗口会移回可见区域。
Tk 版保留基本悬浮窗，不提供托盘入口和锁定功能。

从 0.2.0 起移除任务栏嵌入及 Windhawk 依赖，不再占位、扫描任务栏按钮或等待扩展加载。
旧任务栏配置自动转为悬浮窗；旧快捷方式上的 `--taskbar` / `--floating` 参数仍可启动程序。
新版启动时会退出旧版 AI Fuel 专用的 Windhawk 进程，不影响用户自行安装的 Windhawk。

面板约 **30 秒**检查一次更新。GPT 正常约 30 秒查询一次；Claude 的正常查询间隔
至少 **2 分钟**，间隔内沿用上次成功读数。手动刷新和重启也遵守 Claude 的最小间隔。
两家并行查询，先完成的一家立即更新；失败时各自退避。
同时只允许跑一份（多开会把额度接口打到限流）。
百分比统一表示**已用额度**。右键“额度详情”可查看剩余百分比、数据采样时间和
缓存原因；Qt 版悬停也会显示详情。

查询失败时，五分钟以内的账户缓存显示 `⟳`，右侧显示“缓存”“限流等待”或授权状态。
Claude CLI 访问凭证过期会后台自动续期；桌面端授权由 Claude 桌面端续期，
过期时提示打开 Code 模式，检测到授权更新后自动恢复。授权无法恢复时显示“需授权”，GPT 未登录时显示“需登录”。
**距最后成功查询达到 5 分钟，或额度窗口已重置，主面板就显示 `--`**；旧数值只留在
详情里并注明“不代表当前额度”。这个期限包含程序关闭的时间，重启或手动刷新不会
把旧缓存变新。前端每秒检查有效期，重试等待期间也会按时隐藏过期值。

重置时刻显示**本地绝对时间**：当天显示 `19:19`，其他日期显示 `09-15 02:00`。

## 开机自启

右键窗口 → 勾选**开机自启**。取消勾选即关闭。

它往启动文件夹（`shell:startup`）放一个快捷方式，你在那里能看见、也能自己删。
不写注册表——那属于修改系统启动配置，杀软敏感且用户不易撤销。

勾选状态**直接由快捷方式是否存在决定**，不存配置文件。否则你手动删了快捷方式，
配置还会一直声称"已启用"。

指向哪个目标是自动判断的：跑 exe 时指向 exe 自己，跑源码时指向当前版本的
`run_qt.vbs` 或 `run_tk.vbs`（直接跑 `.py` 会弹控制台）。源码运行时需保留对应的
VBS 启动器。旧版 Tk 曾错误地指向 Qt；若之前已启用，取消勾选后重新勾选即可更新。

诊断用：`aifuel.exe --autostart status|on|off` 不开窗口直接查改，结果写到
`%LOCALAPPDATA%\aifuel\autostart-report.txt`。

## 数据从哪来

**Claude** —— 调 `https://api.anthropic.com/api/oauth/usage`，就是 `/usage` 命令
背后那个接口，拿的是官方真实百分比（`session` 和 `weekly_all`）。

使用 CLI 登录且访问凭证过期时，aifuel 会启动隐藏的官方 Claude Code CLI，通过初始化流程自动续期，
完成后继续查额度。遇到 401 时也会尝试一次官方恢复流程，再查询账户接口。
普通过期无需手动登录或重启；网络失败按退避规则自动重试，只有授权被撤销或无法续期时
才提示“需授权”。临时缓存继续遵守五分钟有效期，Claude 的正常查询间隔仍为两分钟。

凭证文件由 aifuel **只读**，续期的写入、轮换和跨进程锁均交给官方 CLI，避免与其他
Claude Code 会话抢写。助手只发送初始化/额度控制消息，不发送聊天或模型请求，
关闭工具、MCP、用户钩子和会话保存，完成或超时后退出。
凭证路径支持 `CLAUDE_CONFIG_DIR`；助手从 PATH、`~/.local/bin` 或 Windows Claude
桌面端的版本化组件目录找到 `claude`，也可用 `AIFUEL_CLAUDE_EXE` 指定安装位置。
已在 Claude Code 2.1.233 上验证自动续期。

Windows 上没有 CLI 凭证且未指定 `CLAUDE_CONFIG_DIR` 时，使用 Claude 桌面端已保存
的 Code 授权，兼容普通安装和 MSIX 安装的数据目录。通过 Windows DPAPI / CNG
读取桌面端的加密 OAuth 存储，仅在内存中用于额度请求；不读取浏览器 Cookie，
不复制或写入凭证，不替桌面端轮换 refresh token。桌面端授权过期或尚未生成时，
请打开 Claude 的 Code 模式；授权更新后自动恢复，不必重启 AI Fuel。
已有 CLI 凭证时继续使用该账户，不会因过期而切换到桌面端的另一个账户。
桌面端存在多个组织且无法确定目标时会明确提示使用 CLI 登录目标账户。
已在 Claude Desktop 1.52386.3.0 / 内置 Claude Code 2.1.266 上验证桌面端登录查询。

**ChatGPT/Codex** —— 通过已登录的 `codex app-server`，每轮调用官方
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)
查询账户额度。另一台设备使用同一账户后，下一次成功查询即可获得更新，
不需要本机产生新的会话日志。查询复用一个隐藏进程，不创建对话或发起模型请求。

优先选取 `rateLimitsByLimitId.codex` 主额度，避免与 Spark、Reserve 的独立额度混淆。
辅助进程的 SQLite 状态独立存放在 `%LOCALAPPDATA%\aifuel\codex-rpc\`。

> 只有账户查询失败时才使用账户缓存。**主面板不再使用本机会话日志**，也不接受旧版
> 来源不明的缓存。没有账户缓存就显示 `--`。右键详情可看到最后成功时间、失败原因、
> 下次尝试时间，以及已经过期的历史读数。
> `aifuel.exe --diagnose-codex` 不开窗口查询一次，将结果写到运行目录的
> `codex-report.json`；退出码 0 表示实时成功，1 表示使用缓存或查询失败。
> Claude 可使用 `aifuel.exe --diagnose-claude`，结果写入 `claude-report.json`；
> 两种报告仅包含额度和状态，不包含登录凭证。

## 标记含义

| 标记 | 意思 |
|---|---|
| `⟳` | 五分钟有效期内的账户缓存，非实时 |
| `↺` | 该限额窗口已重置，旧数字不再作数 |
| `--` | 没有可信数据，或最后成功查询已超过五分钟 |

颜色：<60% 绿，60–85% 黄，≥85% 红。Claude 还会用服务端的 `severity` 往上抬一档。
阈值在 `display.py` 的 `resolve()` 里。

## 失败与恢复

两家共用缓存、有效期、失败分类和重试规则，状态分别保存，互不阻塞：

- 网络失败、超时、返回格式异常或缺少有效额度字段：保留最后成功的账户缓存，
  30 秒起逐次加倍重试，最长 15 分钟；成功后清除失败状态和退避。
- 接口限流：本地从 90 秒开始加倍，最长 15 分钟；同时遵守服务端 `Retry-After`
  （秒数或 HTTP 日期），取两者中更长的等待。接口返回 `0` 或过短的等待也不会绕过
  本地退避；服务端要求的更长等待不会被截短。Codex app-server 若未转发等待信息，
  则使用本地退避规则。
- 登录失效：提示重新登录，等待期间检查本地凭证变化，有变化就提前尝试。为兼容
  凭证保存在系统密钥库等不可直接观察的情况，至少每 5 分钟再检查一次登录状态。
  Codex 未登录时不会继续请求额度；Claude 的普通凭证过期先自动续期，续期失败则按错误类型重试。
- 手动刷新遵守同一套重试等待，不会绕过接口限流。

## 文件

| 文件 | 作用 |
|---|---|
| `sources.py` | 数据层。单独跑可在终端看一眼 |
| `quota_policy.py` | 共用失败分类、缓存有效期和重试策略 |
| `codex_live.py` | Codex 账户查询与隐藏辅助进程 |
| `claude_auth.py` | Claude 官方 CLI 自动续期与后台助手清理 |
| `claude_desktop.py` | Windows Claude 桌面端组件发现与授权读取 |
| `windows_crypto.py` | 调用 Windows DPAPI / CNG 读取加密数据 |
| `display.py` | 显示决策：什么数字、什么颜色、要不要压暗。两版共用 |
| `widget_qt.py` / `widget_tk.py` | 两个前端 |
| `window_position.py` | 多屏位置恢复 |
| `legacy_cleanup.py` | 退出旧版本遗留的专用扩展进程 |
| `make_icon.py` | 生成 `aifuel.ico` |
| `build.ps1` | 打包。加 `-Debug` 出带控制台的版本，能看崩溃回溯 |

运行时状态（缓存、窗口位置）在 `%LOCALAPPDATA%\aifuel\`，**不在程序目录**——
打包后程序目录可能没写权限。删掉那个文件夹即可重置一切。

## 重新打包

### Windows 安装包

构建机器安装 [Inno Setup 6](https://jrsoftware.org/isdl.php)（已验证 6.7.3）后，
双击项目根目录的 **`build-installer.cmd`**。窗口会保留构建结果，不需要记住
PowerShell 命令或永久修改执行策略。Python 和依赖仍需与下方便携版构建环境一致。

产物在 `dist\installer\AI-Fuel-0.2.3-windows-x64-setup.exe`，旁边的 `.sha256`
文件用于校验。发布时只需分发这个安装包，用户无需 Python、编译器或 PowerShell 操作。
指定版本可运行 `build-installer.cmd -Version 0.2.0`；自定义编译器位置使用
`-IsccPath "C:\工具目录\ISCC.exe"`。

安装包默认安装到 `%LOCALAPPDATA%\Programs\AI Fuel`，仅对当前用户生效，无需管理员权限。
提供开始菜单入口和可选桌面快捷方式；开机自启在程序菜单中控制。升级前需退出正在运行的
AI Fuel（含便携版）。后续版本保持同一安装标识，覆盖安装即可升级。
安装时，已有的 AI Fuel 自启快捷方式会迁移到新版安装路径；未启用自启的用户保持关闭。
升级会移除旧安装目录中的任务栏扩展。退出取数期间再次打开会恢复原窗口和请求，避免等待网络超时才重新显示。
卸载入口在 Windows“已安装的应用”中；卸载保留 `%LOCALAPPDATA%\aifuel` 下的个人配置和缓存，
只清理指向本次安装路径的开机自启快捷方式。账户登录前提与便携版相同。

安装包构建先在 `build\installer-payload` 生成完整程序，包含托盘图标，
再用 `installer\aifuel.iss` 制作安装向导。简体中文语言文件来自
[Inno Setup 6.7.3 仓库](https://github.com/jrsoftware/issrc/blob/is-6_7_3/Files/Languages/Unofficial/ChineseSimplified.isl)，
保留原作者信息。当前安装包未做代码签名，发布签名与现有便携版的要求一致。

构建后可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File tests\smoke_installer.ps1`
验证真实安装、快捷方式、运行中阻止安装、程序启动、升级和卸载。
测试使用独立产品标识及 `build` 下的中文安装路径，检查文件哈希、个人数据和原有自启项，
完成后卸载测试产品，日志保留在 `build\aifuel-smoke-*`。

### 便携版

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

排除模块时**别碰 `email` / `http` / `xml`**：`urllib.request` 靠 `email.message`
解析 HTTP 头，排掉它 exe 会在启动时静默崩溃（`--windowed` 会把回溯吞掉）。
调试用 `-Debug`。

构建脚本会临时限制 PATH 为 Python 和 Windows 目录，避免把 Poppler 等工具的
同名 DLL 误打进 exe，造成源码能跑、打包后 QtCore 加载失败。

## 回归测试

```powershell
python -m unittest discover -s tests -v
```

测试使用隔离的缓存和模拟请求，不调用额度接口或修改真实开机自启。
安装 PySide6 后会同时验证 Qt 线程释放和退出；未安装时自动跳过 Qt 测试。
Windows 上若已有 `dist\aifuel\aifuel.exe`，还会验证打包程序的启动诊断入口。
Windows 11 上还会在独立测试进程中启用目录链接保护，验证安装后查询组件的
自动发现；测试使用临时文件，不联网、不改变系统保护设置。

## 编码约定（改代码前必读）

Windows 上三种脚本的读法各不相同，搞错会得到莫名其妙的语法错误：

| 类型 | 必须 | 搞错的后果 |
|---|---|---|
| `.py` | UTF-8，**不要** BOM | 带 BOM 时 `ast.parse` 报 `invalid non-printable character U+FEFF` |
| `.vbs` | **纯 ASCII** | WScript 按系统 ANSI 页读，中文会被误解码并吞掉引号 → `未结束的字符串常量` |
| `.ps1` | UTF-8，**必须带** BOM | PowerShell 5.1 无 BOM 时按 ANSI 读，同样吞引号 → `Missing closing '}'` |

所以 `.vbs` 里的注释和提示语一律用英文。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 已知限制

**ChatGPT 的窗口标签由 `windowDurationMins` 推出，不看键名。** 不同套餐的
额度结构不一样：`plus` 的 `primary` 是 300 分钟（5 小时窗），
而 `prolite` 的 `primary` 是 10080 分钟（周窗）且 `secondary` 为 `null`。
按 `primary`/`secondary` 硬编码标签会把周额度标成 5h。

**GPT 行显示 Codex 主额度。** 账户接口还可能返回 Spark、Reserve 等独立额度，
当前不会混入主额度行；它也不是所有 ChatGPT 产品限额的汇总。

- Claude 那个接口是非公开的，Claude Code 大版本更新后可能变。真变了的话，
  `sources.py` 里 `USAGE_URL` / `OAUTH_BETA` 是要改的地方。
- 125% 等 DPI 缩放下正常（布局按窗口实际逻辑尺寸算）。
