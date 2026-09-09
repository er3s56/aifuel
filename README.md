# aifuel

AI 额度悬浮窗。同时盯 Claude 和 ChatGPT/Codex 的用量，置顶小窗，不占地方。

```
CL   5h   13%  ▬▬▭▭▭▭▭▭   4h
     wk   11%  ▬▬▭▭▭▭▭▭   5d
GPT  5h   --↺  ▭▭▭▭▭▭▭▭
     wk    2%  ▭▭▭▭▭▭▭▭   6d
```

## 用哪个

| 方式 | 需要什么 | 说明 |
|---|---|---|
| **`dist\aifuel\aifuel.exe`** | 什么都不用装 | 独立打包，双击即用。整个 `dist\aifuel` 文件夹一起拷走 |
| `run_qt.vbs` | Python + PySide6 | 开发时用，改完代码直接跑 |
| `run_tk.vbs` | Python（tkinter 自带） | 零依赖备选，外观朴素些 |

> **前提**：机器上得装了 Claude Code / Codex 并登录过。exe 打包的是程序，
> 不是你的账号——读不到 `~/.claude` 和 `~/.codex` 就只会显示 `--`。

> **杀软**：未签名的 PyInstaller 产物可能被 Defender / SmartScreen 拦，
> 需要手动点“仍要运行”。根治要代码签名证书。

## 操作

| 动作 | 效果 |
|---|---|
| 左键拖动 | 移动窗口（位置自动记住） |
| 双击 | 立即刷新 |
| 右键 | 菜单：刷新 / 不透明度 / 退出 |
| 鼠标悬停 | Qt 版自动变淡，方便看底下的东西 |

**5 分钟**刷新一次。同时只允许跑一份（多开会把额度接口打到限流）。

## 开机自启

`Win+R` 输入 `shell:startup`，把 `aifuel.exe` 的**快捷方式**拖进去。

## 数据从哪来

**Claude** —— 调 `https://api.anthropic.com/api/oauth/usage`，就是 `/usage` 命令
背后那个接口，拿的是官方真实百分比（`session` 和 `weekly_all`）。

> 对 `~/.claude/.credentials.json` **只读，永不写入**。刷新 OAuth token 会轮换
> refresh token，如果和 Claude Code 同时写就可能把你登出。所以 token 过期时
> 显示上次缓存值并标 `⟳`，下次你用 Claude Code 时自动恢复。
> 想手动恢复：跑一次 `claude -p "hi"`。

**ChatGPT/Codex** —— 读 `~/.codex/sessions/**/*.jsonl` 里最后一条 `token_count`
事件的 `rate_limits`。纯本地，不发网络请求。

> 代价是这份数据只新鲜到你上次用 Codex 为止。窗口若已滚过去，日志里的百分比
> 就是错的——此时显示 `--↺` 而不是一个会骗人的数字。

## 标记含义

| 标记 | 意思 |
|---|---|
| `⟳` | 磁盘缓存值，非实时 |
| `↺` | 该限额窗口已重置，旧数字不再作数 |
| `--` | 取不到数据 |

颜色：<60% 绿，60–85% 黄，≥85% 红。Claude 还会用服务端的 `severity` 往上抬一档。
阈值在 `display.py` 的 `resolve()` 里。

## 限流保护

`/api/oauth/usage` 自己有速率限制。撞到 429 会指数退避（5 分钟起，封顶 30 分钟），
期间不再发任何请求，只显示缓存值。窗口上表现为一直带 `⟳`。这不是坏了。

## 文件

| 文件 | 作用 |
|---|---|
| `sources.py` | 数据层。单独跑可在终端看一眼 |
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

- Claude 那个接口是非公开的，Claude Code 大版本更新后可能变。真变了的话，
  `sources.py` 里 `USAGE_URL` / `OAUTH_BETA` 是要改的地方。
- 125% 等 DPI 缩放下正常（布局按窗口实际逻辑尺寸算）。
