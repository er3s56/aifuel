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
| **`dist\aifuel\aifuel.exe`** | 无需 Python；登录要求见下方 | 独立打包，双击即用。整个 `dist\aifuel` 文件夹一起拷走 |
| `run_qt.vbs` | Python + PySide6 | 开发时用，改完代码直接跑 |
| `run_tk.vbs` | Python（tkinter 自带） | 零依赖备选，外观朴素些 |

> **前提**：机器上得装了 Claude Code / Codex 并登录过。exe 打包的是程序，
> 不是你的账号——读不到 `~/.claude` 和 `~/.codex` 就只会显示 `--`。
> GPT 实时查询还需要可运行的 Codex CLI。默认查找 PATH 和 Windows 安装器目录；
> 非标准安装可用 `AIFUEL_CODEX_EXE` 指定可执行文件路径。

> **杀软**：未签名的 PyInstaller 产物可能被 Defender / SmartScreen 拦，
> 需要手动点“仍要运行”。根治要代码签名证书。

## 操作

| 动作 | 效果 |
|---|---|
| 左键拖动 | 移动窗口（位置自动记住） |
| 双击 | 立即刷新 |
| 右键 | 菜单：刷新 / 额度详情 / 不透明度 / 开机自启 / 退出 |

正常约 **30 秒**刷新一次。两家并行查询，先完成的一家立即更新；失败时各自退避。
同时只允许跑一份（多开会把额度接口打到限流）。
百分比统一表示**已用额度**。右键“额度详情”可查看剩余百分比、数据采样时间和
缓存原因；Qt 版悬停也会显示详情。

查询失败时，五分钟以内的账户缓存显示 `⟳`，右侧显示“缓存”“限流等待”或“需登录”。
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

> 对 `~/.claude/.credentials.json` **只读，永不写入**。刷新 OAuth token 会轮换
> refresh token，如果和 Claude Code 同时写就可能把你登出。所以 token 过期时
> 提示“需登录”，短期缓存仍按统一的五分钟规则处理。打开 Claude Code 更新登录后，
> aifuel 检测到凭证变化，会在下一轮检查时提前恢复查询，无需重启。

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
- 接口限流：优先遵守服务端 `Retry-After`（秒数或 HTTP 日期）；未提供时从 90 秒
  开始加倍，最长 15 分钟。服务端明确要求的更长等待不会被截短。Codex app-server
  若未转发等待信息，则使用本地退避规则。
- 登录失效：提示重新登录，等待期间检查本地凭证变化，有变化就提前尝试。为兼容
  凭证保存在系统密钥库等不可直接观察的情况，至少每 5 分钟再检查一次登录状态。
  Codex 未登录时不会继续请求额度；aifuel 不主动发起登录或强制刷新令牌。
- 手动刷新遵守同一套重试等待，不会绕过接口限流。

## 文件

| 文件 | 作用 |
|---|---|
| `sources.py` | 数据层。单独跑可在终端看一眼 |
| `quota_policy.py` | 共用失败分类、缓存有效期和重试策略 |
| `codex_live.py` | Codex 账户查询与隐藏辅助进程 |
| `display.py` | 显示决策：什么数字、什么颜色、要不要压暗。两版共用 |
| `widget_qt.py` / `widget_tk.py` | 两个前端 |
| `make_icon.py` | 生成 `aifuel.ico` |
| `build.ps1` | 打包。加 `-Debug` 出带控制台的版本，能看崩溃回溯 |

运行时状态（缓存、窗口位置）在 `%LOCALAPPDATA%\aifuel\`，**不在程序目录**——
打包后程序目录可能没写权限。删掉那个文件夹即可重置一切。

## 重新打包

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
