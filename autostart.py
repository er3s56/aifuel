# -*- coding: utf-8 -*-
"""开机自启：往启动文件夹放/删一个快捷方式。

选启动文件夹而不是注册表 Run 键，是因为它不需要管理员权限、用户在
shell:startup 里看得见也能自己删、且杀软对它最不敏感。

状态不落配置文件，直接由「快捷方式是否存在」决定 —— 否则用户手动删掉
快捷方式后，配置还会一直声称"已启用"，两份真相必然漂移。
"""
from __future__ import annotations

import os
import subprocess
import sys

LINK_NAME = "aifuel.lnk"


def startup_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def link_path() -> str:
    return os.path.join(startup_dir(), LINK_NAME)


def _here() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def target(frontend: str = "qt") -> "tuple[str, str]":
    """返回 (要启动的东西, 工作目录)。

    打包成 exe 时就指向自己；源码运行时指向当前前端的 .vbs 启动器。
    """
    here = _here()
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), here
    if frontend not in ("qt", "tk"):
        raise ValueError("未知前端: %s" % frontend)
    return os.path.join(here, "run_%s.vbs" % frontend), here


def is_enabled() -> bool:
    return os.path.isfile(link_path())


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串的转义：内部单引号写成两个。"""
    return s.replace("'", "''")


def _make_shortcut(link: str, tgt: str, workdir: str, icon: str = "") -> None:
    """建快捷方式，走 PowerShell 的 WScript.Shell COM。

    不用 pywin32：它能省下一次进程启动，但会让源码模式和 exe 模式走不同代码
    （exe 里 pywin32 要么额外打进 7.7MB、要么缺失走兜底），等于总有一条路径
    没被真正测过。切换自启是个罕见的用户动作，多花一秒无所谓。
    PowerShell 在 Win10/11 上一定存在。
    """
    if tgt.lower().endswith(".vbs"):
        # VBS 用 wscript 拉起（且不弹控制台）；Python 脚本不能交给 wscript。
        ps_target = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                 "System32", "wscript.exe")
        ps_args = '"%s"' % tgt
    else:
        ps_target, ps_args = tgt, ""

    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';"
        "$s.WindowStyle=7;%s$s.Save()"
    ) % (_ps_quote(link), _ps_quote(ps_target), _ps_quote(ps_args),
         _ps_quote(workdir),
         ("$s.IconLocation='%s';" % _ps_quote(icon)) if icon else "")

    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def enable(frontend: str = "qt") -> "tuple[bool, str]":
    """开启自启。返回 (成功?, 给用户看的说明)。"""
    if sys.platform != "win32":
        return False, "只支持 Windows"
    tgt, workdir = target(frontend)
    if not os.path.exists(tgt):
        return False, "找不到启动目标：\n%s\n请保留对应的 VBS 启动器，或重新打包 exe。" % tgt
    try:
        os.makedirs(startup_dir(), exist_ok=True)
        icon = os.path.join(_here(), "aifuel.ico")
        _make_shortcut(link_path(), tgt, workdir, icon if os.path.isfile(icon) else "")
    except Exception as e:
        return False, "创建快捷方式失败：%s" % e
    if not is_enabled():
        return False, "快捷方式没能写入启动文件夹"
    return True, "已开启。开机会自动启动：\n%s" % tgt


def disable() -> "tuple[bool, str]":
    try:
        if os.path.isfile(link_path()):
            os.remove(link_path())
    except Exception as e:
        return False, "删除快捷方式失败：%s" % e
    return not is_enabled(), "已关闭开机自启"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tgt, wd = target()
    print("启动文件夹:", startup_dir())
    print("快捷方式  :", link_path())
    print("启动目标  :", tgt, "(存在:", os.path.exists(tgt), ")")
    print("工作目录  :", wd)
    print("当前状态  :", "已开启" if is_enabled() else "未开启")
    if len(sys.argv) > 1:
        ok, msg = enable() if sys.argv[1] == "on" else disable()
        print("\n%s -> %s\n%s" % (sys.argv[1], "OK" if ok else "失败", msg))
