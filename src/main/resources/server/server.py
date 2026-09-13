# -*- coding: utf-8 -*-
"""
云端速递 · 整合包云更新服务端（独立版）

背景：早期开发时把「云端速递」的云更新功能直接塞进了 mod2（生存作弊小套餐）
共用的 verify_server.py。本文件是从中独立出来的「云端速递」专属服务端：
只提供整合包云更新能力（TCP 指令协议）+ 专属 Web 管理端（默认 HTTP 10010）。

功能：
  * 唯一 TCP 端口（默认 10086，可用配置文件 speedupdate.conf 修改）同时提供：
      GET_VERSION                -> {"version":"…","changelog":"…"}
      GET_FEEDBACK_URL           -> {"feedbackUrl":"https://chat.deepseek.com/"}
      GET_MANIFEST|mods          -> {"mods/xx.jar":"sha1",…}
      GET_MANIFEST|shaderpacks   -> {"shaderpacks/xx.zip":"sha1",…}
      GET_MANIFEST|resourcepacks -> {"resourcepacks/xx.zip":"sha1",…}
      GET_FILENAME|<sha1>        -> 相对路径（未找到返回空行）
      GET_FILE|<相对路径>         -> 一行 FILE|<字节数> + 原始字节流（传完即断）
  * 配置文件 speedupdate.conf：一行一个“键=值”，# 开头为注释，UTF-8 中文注释；
    首次运行自动生成（此时若同目录存在旧版 version.txt，会把其版本号带入配置）。
      配置项：
        port            TCP 服务端口（默认 10086）
        version         整合包内容版本号（默认 1.0.000）
        bind_v4         IPv4 监听地址（默认 0.0.0.0 = 全部 IPv4 地址；
                        可改 127.0.0.1 = 仅本机，或指定某个 IPv4 地址）
        enable_v6       是否启用 IPv6 监听（默认 true = 监听全部 IPv6 地址；
                        改 false = 不监听 IPv6）
        max_downloads   文件下载最大并发数（默认 10，范围 1~64）
        feedback_url    客户端「问题反馈」按钮跳转链接（默认 https://chat.deepseek.com/，
                        客户端每次更新完成后会自动同步此配置）
  * 启动时自动执行原 rename_to_version.bat 的逻辑（纯 Python 实现，每次启动先执行）：
      mods/*.jar、shaderpacks/*.zip 与 resourcepacks/*.zip 统一追加 "_v<版本号>_ZAKO" 尾缀，
      示例：lionfishapi-3.1.jar -> lionfishapi-3.1_v8.19.526_ZAKO.jar
      旧尾缀（_v<版本> 或 _v<版本>_ZAKO）自动替换为新版本号，可重复运行不叠加；
      只处理目录顶层文件（与脚本一致），支持中文等任意 Unicode 文件名；
      其余文件与文件夹一律不动。
  * 完整运行日志 speedupdate.log：不存在则自动生成，已存在则启动时清空重建；
    详尽记录所有运行逻辑（连接/指令/回复内容含完整清单哈希/下载明细/异常堆栈），
    确保出现问题可以用日志完整复盘当时工况。
  * Web 管理端（默认 HTTP 10010；端口在配置 admin_port，设 0 = 关闭；监听地址复用 bind_v4/enable_v6）：
      登录鉴权（密码见配置 admin_password，登录状态 24 小时，服务端重启即全部失效）；
      分发文件上传 / 改名 / 删除（仅顶层文件、扩展名白名单、临时文件原子替换）；
      内容版本号与 Changelog 在线编辑；全部配置项在线修改；运行日志与审计日志查看；
      一键「重新加载」（重读配置 → 检查目录 → 重跑重命名 → 重建哈希清单），不影响玩家连接。
      安全：除登录页与登录接口外，所有请求都必须携带有效会话 Cookie；
      密码比对使用常数时间比较；同一 IP 连续失败 5 次锁定 5 分钟；全部管理操作写入审计日志。
  * 可靠性设计：有界线程池处理连接（防线程爆炸）、下载并发信号量限流、
    哈希清单由后台线程增量重建（服务请求零阻塞）、socket 超时兜底、
    所有工作线程 daemon 化、配置读取线程安全。

使用方法：双击运行本文件（需要 Python 3）。修改配置后重启生效。
更新内容：mods/ 放 .jar、shaderpacks/ 与 resourcepacks/ 放 .zip，版本号改 speedupdate.conf 的 version，
更新日志写 Changelog.txt（可选，不存在则返回空日志）。
"""
import codecs
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ========== 路径与默认值 ==========
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "speedupdate.conf")
LOG_FILE = os.path.join(SCRIPT_DIR, "speedupdate.log")
CHANGELOG_FILE = os.path.join(SCRIPT_DIR, "Changelog.txt")
MODS_DIR = os.path.join(SCRIPT_DIR, "mods")
SHADERPACKS_DIR = os.path.join(SCRIPT_DIR, "shaderpacks")
RESOURCEPACKS_DIR = os.path.join(SCRIPT_DIR, "resourcepacks")
# 旧版版本文件：仅用于「首次生成配置文件」时迁移版本号，之后不再读写
LEGACY_VERSION_FILE = os.path.join(SCRIPT_DIR, "version.txt")

DEFAULT_PORT = 10086
DEFAULT_VERSION = "1.0.000"
DEFAULT_BIND_V4 = "0.0.0.0"
DEFAULT_ENABLE_V6 = True
DEFAULT_MAX_DOWNLOADS = 10
DEFAULT_FEEDBACK_URL = "https://chat.deepseek.com/"

# 分发目录与允许的扩展名：mods 只认 .jar、shaderpacks/resourcepacks 只认 .zip
MANIFEST_RULES = (
    (MODS_DIR, ".jar"),
    (SHADERPACKS_DIR, ".zip"),
    (RESOURCEPACKS_DIR, ".zip"),
)
# 重命名规则：目录名 -> 扩展名（与 MANIFEST_RULES 目录一一对应）
RENAME_RULES = (
    ("mods", ".jar"),
    ("shaderpacks", ".zip"),
    ("resourcepacks", ".zip"),
)
# GET_MANIFEST 指令允许的清单类型（自动取自分发目录名）
MANIFEST_KINDS = tuple(os.path.basename(root) for root, _ext in MANIFEST_RULES)

CONNECT_READ_TIMEOUT = 15.0   # 指令读取阶段超时（秒），防恶意连接挂线程
FILE_SEND_TIMEOUT = 600.0     # 文件传输阶段超时（秒），大文件慢网放宽
FILE_SEND_CHUNK = 64 * 1024   # 发送缓冲
SHA_CHUNK = 1024 * 1024       # 哈希计算缓冲
MAX_COMMAND_LEN = 2048        # 单条指令行最大长度（防御超长行）
BACKLOG = 128                 # listen 队列
MAX_WORKERS = 64              # 连接处理线程池上限
MAX_PENDING_CONNS = 64        # 排队等待处理的最大连接数（超出直接拒绝）
WATCH_INTERVAL = 5.0          # 清单后台监测间隔（秒）

# ---------- Web 管理端（默认 10010；监听地址复用 bind_v4 / enable_v6） ----------
SERVER_VERSION = "3.0.0"                      # 服务端脚本版本（与整合包内容版本是两套体系）
DEFAULT_ADMIN_PORT = 10010                    # 管理端端口；0 = 关闭管理端
DEFAULT_ADMIN_PASSWORD = "12345678"           # 默认管理密码（明文存配置文件，便于随时修改）
ADMIN_AUDIT_FILE = os.path.join(SCRIPT_DIR, "speedupdate_admin.log")  # 审计日志（追加写，不随启动清空）
ADMIN_UPLOAD_TMP = os.path.join(SCRIPT_DIR, "_upload_tmp")            # 上传临时目录（不在分发目录内）
ADMIN_SESSION_TTL = 24 * 3600                 # 登录状态有效期（秒）
ADMIN_LOGIN_MAX_FAIL = 5                      # 同一 IP 连续失败上限
ADMIN_LOGIN_LOCK_SEC = 300                    # 达到上限后的锁定时长（秒）
ADMIN_MAX_UPLOAD = 4 * 1024 * 1024 * 1024     # 单文件上传上限（4 GB）
ADMIN_BODY_TIMEOUT = 20.0                     # HTTP 请求体读写 socket 超时（秒）
ADMIN_LOG_PAGE_LINES = 300                    # 日志接口单次返回的最大行数
ADMIN_COOKIE_NAME = "ce_sid"                  # 会话 Cookie 名
ADMIN_RELOAD_STEPS = ("重读 speedupdate.conf 配置", "检查分发目录与 Changelog.txt",
                      "执行文件重命名（_v<版本>_ZAKO）", "重建哈希清单", "完成")
ADMIN_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\u202a-\u202e\u2066-\u2069]')  # 非法字符 + bidi 控制符
ADMIN_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)]
)

# 管理端页面（单文件内联 HTML，零外部依赖；由构建脚本注入真实页面内容）
ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>云端速递 · 管理端</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    :root {
        --bg: #e8e8e8; --bar: #3a3a3a; --card: #f5f5f5; --line: #ccc; --line-soft: #e4e4e4;
        --text: #333; --text-2: #666; --text-3: #999;
        --btn: #d0d0d0; --btn-h: #b8b8b8; --dark: #4a4a4a; --dark-h: #383838;
        --danger: #e74c3c; --danger-h: #c0392b; --ok: #4caf50; --warn: #e6a23c;
        --mono: 'Consolas', 'Cascadia Mono', 'Courier New', monospace;
    }
    body { font-family: 'Segoe UI', Roboto, Arial, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; min-height: 100vh; }
    .hidden { display: none !important; }
    .mono { font-family: var(--mono); font-size: 0.82rem; color: #555; }
    .dim { color: var(--text-3); }
    .warn-text { color: #a26a00; }

    .topbar { position: fixed; top: 0; left: 0; right: 0; height: 50px; background: var(--bar); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; z-index: 900; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
    .topbar .brand { color: #eee; font-size: 1.05rem; font-weight: 300; letter-spacing: 0.5px; }
    .topbar .brand b { font-weight: 500; }
    .topbar .nav { display: flex; gap: 18px; align-items: center; }
    .topbar .nav a { color: #ccc; text-decoration: none; font-size: 0.92rem; padding: 4px 0; border-bottom: 2px solid transparent; transition: color 0.2s, border-color 0.2s; cursor: pointer; white-space: nowrap; }
    .topbar .nav a:hover { color: #fff; }
    .topbar .nav a.active { color: #fff; border-bottom-color: #999; }
    .topbar .nav .logout { color: #bbb; font-size: 0.88rem; margin-left: 6px; }

    .main { max-width: 1100px; margin: 0 auto; padding: 74px 20px 50px; }
    h1 { font-size: 1.5rem; font-weight: 300; color: #333; margin-bottom: 6px; letter-spacing: 0.5px; }
    .page-desc { color: var(--text-2); font-size: 0.88rem; margin-bottom: 20px; }

    .card { background: var(--card); border: 1px solid var(--line-soft); border-radius: 8px; padding: 18px 20px; margin-bottom: 16px; }
    .card > h2 { font-size: 1rem; font-weight: 500; color: #444; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--line-soft); }
    .card > h2 .hint { font-weight: 400; font-size: 0.82rem; color: var(--text-3); margin-left: 8px; }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
    .stat { background: #fff; border: 1px solid var(--line-soft); border-radius: 6px; padding: 14px 16px; }
    .stat .k { font-size: 0.8rem; color: var(--text-3); margin-bottom: 6px; }
    .stat .v { font-size: 1.35rem; font-weight: 400; color: #333; letter-spacing: 0.5px; }
    .stat .v small { font-size: 0.78rem; color: var(--text-3); font-weight: 400; margin-left: 4px; }
    .stat .s { font-size: 0.78rem; color: var(--text-3); margin-top: 6px; }
    .kv { display: grid; grid-template-columns: 150px 1fr; gap: 8px 12px; font-size: 0.9rem; }
    .kv .k { color: var(--text-2); }
    .kv .v { color: #333; word-break: break-all; }

    .btn { padding: 8px 16px; border: 1px solid transparent; border-radius: 6px; background: var(--btn); color: #333; font-size: 0.88rem; font-weight: 500; cursor: pointer; font-family: inherit; transition: background 0.2s; white-space: nowrap; }
    .btn:hover { background: var(--btn-h); }
    .btn-primary { background: var(--dark); color: #fff; }
    .btn-primary:hover { background: var(--dark-h); }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:hover { background: var(--danger-h); }
    .btn-sm { padding: 4px 10px; font-size: 0.8rem; }
    .btn:disabled { opacity: 0.5; cursor: default; }
    .btn-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

    label.fl { display: block; font-size: 0.85rem; color: var(--text-2); margin-bottom: 5px; font-weight: 500; }
    input[type=text], input[type=password], textarea, select { width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 6px; font-size: 0.92rem; background: #fff; font-family: inherit; color: var(--text); transition: border 0.2s; }
    input:focus, textarea:focus, select:focus { border-color: #888; outline: none; }
    .field { margin-bottom: 15px; }
    .field .tip { font-size: 0.78rem; color: var(--text-3); margin-top: 5px; line-height: 1.5; }
    .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    @media (max-width: 700px) { .row2 { grid-template-columns: 1fr; } }
    .checkline { display: flex; align-items: flex-start; gap: 8px; font-size: 0.88rem; color: var(--text-2); }
    .checkline input { width: auto; margin-top: 3px; }

    table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
    th { text-align: left; font-weight: 500; color: var(--text-2); padding: 9px 10px; border-bottom: 1px solid #ddd; background: #ececec; white-space: nowrap; }
    td { padding: 8px 10px; border-bottom: 1px solid #e8e8e8; vertical-align: middle; }
    tbody tr:hover { background: #ececec; }
    .fname { word-break: break-all; }
    .ops { display: flex; gap: 6px; }

    .tabs { display: flex; gap: 2px; border-bottom: 1px solid #ddd; margin-bottom: 16px; flex-wrap: wrap; }
    .tab { padding: 8px 16px; cursor: pointer; font-size: 0.9rem; color: var(--text-2); border: none; border-bottom: 2px solid transparent; background: none; font-family: inherit; }
    .tab:hover { color: #333; }
    .tab.active { color: #222; border-bottom-color: #555; font-weight: 500; }

    .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.74rem; background: #e4e4e4; color: #555; white-space: nowrap; }
    .badge.ok { background: #e6f4ea; color: #2e7d32; }
    .badge.warn { background: #fdf1dc; color: #a26a00; }
    .badge.dim { background: #e6e6e6; color: #999; }
    .badge.req { background: #fbe6e3; color: #b03a2e; }

    .drop { border: 2px dashed var(--line); border-radius: 8px; padding: 22px; text-align: center; color: var(--text-2); font-size: 0.9rem; background: #fafafa; transition: border-color 0.2s, background 0.2s; cursor: pointer; }
    .drop:hover, .drop.over { border-color: #888; background: #f2f2f2; }
    .drop .big { font-size: 0.95rem; color: #444; margin-bottom: 4px; }
    .drop .sub { font-size: 0.78rem; color: var(--text-3); }
    .queue { margin-top: 14px; }
    .qitem { display: grid; grid-template-columns: 1fr 90px 150px 80px; gap: 10px; align-items: center; padding: 8px 10px; background: #fff; border: 1px solid var(--line-soft); border-radius: 6px; margin-bottom: 6px; font-size: 0.85rem; }
    .bar { height: 6px; background: #e0e0e0; border-radius: 3px; overflow: hidden; }
    .bar > i { display: block; height: 100%; background: var(--dark); width: 0; transition: width 0.25s; }
    .qitem .bar > i.err { background: var(--danger); }

    .steps { font-size: 0.88rem; }
    .step { display: flex; align-items: center; gap: 10px; padding: 5px 0; color: var(--text-3); }
    .step .dot { width: 16px; height: 16px; border-radius: 50%; background: #ddd; display: flex; align-items: center; justify-content: center; font-size: 0.7rem; color: #fff; flex: 0 0 16px; }
    .step.doing { color: #333; }
    .step.doing .dot { background: var(--warn); }
    .step.done { color: #2e7d32; }
    .step.done .dot { background: var(--ok); }
    .step.fail { color: #c0392b; }
    .step.fail .dot { background: var(--danger); }

    .logbox { background: #fbfbfb; border: 1px solid var(--line-soft); border-radius: 6px; height: 520px; overflow-y: auto; padding: 10px 12px; font-family: var(--mono); font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-all; color: #444; }
    .logbox .lv-i { color: #35708f; }
    .logbox .lv-w { color: #a26a00; }
    .logbox .lv-e { color: #c0392b; }
    .logbox .hi { background: #fff3b0; }

    .login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
    .login-card { background: var(--card); border: 1px solid var(--line-soft); border-radius: 12px; width: 100%; max-width: 380px; padding: 32px 28px; box-shadow: 0 4px 18px rgba(0,0,0,0.08); }
    .login-card .logo { text-align: center; margin-bottom: 24px; }
    .login-card .logo .t1 { font-size: 1.4rem; font-weight: 300; color: #333; letter-spacing: 1px; }
    .login-card .logo .t2 { font-size: 0.82rem; color: var(--text-3); margin-top: 6px; letter-spacing: 0.5px; }
    .login-card .btn { width: 100%; margin-top: 8px; padding: 11px; }
    .login-foot { text-align: center; font-size: 0.8rem; color: var(--text-3); margin-top: 18px; line-height: 1.6; }
    .login-foot a { color: #888; cursor: pointer; border-bottom: 1px solid #c8c8c8; padding-bottom: 1px; transition: color 0.2s, border-color 0.2s; }
    .login-foot a:hover { color: #555; border-bottom-color: #999; }

    .mask { position: fixed; inset: 0; background: rgba(0,0,0,0.35); display: flex; align-items: center; justify-content: center; z-index: 9999; padding: 20px; }
    .modal { background: var(--card); border-radius: 8px; width: 100%; max-width: 470px; padding: 22px; box-shadow: 0 8px 30px rgba(0,0,0,0.25); }
    .modal h3 { font-size: 1.05rem; font-weight: 500; margin-bottom: 12px; color: #333; }
    .modal .mbody { font-size: 0.9rem; color: var(--text-2); line-height: 1.7; }
    .modal .mbody p { margin-bottom: 8px; }
    .modal .mbody .box { background: #fff; border: 1px solid var(--line-soft); border-radius: 6px; padding: 10px 12px; margin: 8px 0; }
    .modal-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }

    .toast { position: fixed; bottom: 28px; right: 28px; min-width: 220px; max-width: 380px; padding: 14px 22px; border-radius: 8px; color: #fff; font-size: 0.9rem; font-weight: 500; box-shadow: 0 4px 16px rgba(0,0,0,0.15); opacity: 0; transform: translateY(20px); transition: opacity 0.35s ease, transform 0.35s ease; pointer-events: none; z-index: 10000; line-height: 1.5; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.success { background: var(--ok); }
    .toast.error { background: #f44336; }
    .toast.warn { background: var(--warn); }
    .notice { background: #fdf1dc; border: 1px solid #f0dcae; color: #8a6200; border-radius: 6px; padding: 9px 14px; font-size: 0.83rem; margin-bottom: 18px; }
    @media (max-width: 700px) {
        .main { padding: 70px 14px 40px; }
        .qitem { grid-template-columns: 1fr 60px; }
        .toast { bottom: 18px; right: 18px; left: 18px; max-width: none; }
    }
</style>
</head>
<body>

<div id="fileNotice" class="hidden" style="padding:40px;text-align:center;color:#a26a00;">
    请通过服务端地址访问管理端（例如 http://服务器IP:10010），不要直接双击打开本文件。
</div>

<div id="loginPage" class="login-wrap">
    <div class="login-card">
        <div class="logo">
            <div class="t1">云端速递</div>
            <div class="t2">Cloud Express · 管理端</div>
        </div>
        <div class="field">
            <label class="fl" for="loginPwd">管理密码</label>
            <input type="password" id="loginPwd" placeholder="请输入管理密码" autocomplete="off">
        </div>
        <button class="btn btn-primary" id="loginBtn">登 录</button>
        <div class="login-foot"><a id="forgotBtn">忘记密码？</a></div>
    </div>
</div>

<div id="app" class="hidden">
    <div class="topbar">
        <div class="brand">云端速递 <b>· 管理端</b></div>
        <div class="nav">
            <a data-nav="overview">概览</a>
            <a data-nav="files">文件管理</a>
            <a data-nav="version">版本与日志</a>
            <a data-nav="config">配置</a>
            <a data-nav="logs">运行日志</a>
            <a class="logout" id="logoutBtn">退出</a>
        </div>
    </div>

    <div class="main">
        <div id="rebuildNotice" class="notice hidden">哈希清单正在后台重建（全量重算），期间文件列表可能不是最新，请稍候…</div>

        <!-- 概览 -->
        <section id="page-overview" class="page">
            <h1>概览</h1>
            <div class="page-desc">服务状态、分发文件概况与清单状态</div>

            <div class="grid" style="margin-bottom:16px;">
                <div class="stat">
                    <div class="k">整合包内容版本</div>
                    <div class="v" id="ovVersion">—</div>
                    <div class="s">客户端据此判断是否需要更新</div>
                </div>
                <div class="stat">
                    <div class="k">分发文件</div>
                    <div class="v" id="ovFileCount">—</div>
                    <div class="s" id="ovFileSize">—</div>
                </div>
                <div class="stat">
                    <div class="k">今日连接客户端</div>
                    <div class="v" id="ovClientIps">—</div>
                    <div class="s" id="ovClientConns">—</div>
                </div>
                <div class="stat">
                    <div class="k">当前下载并发</div>
                    <div class="v" id="ovDownload">—</div>
                    <div class="s">上限由 max_downloads 控制</div>
                </div>
            </div>

            <div class="card">
                <h2>服务信息</h2>
                <div class="kv">
                    <div class="k">玩家更新端口</div><div class="v mono" id="ovListen">—</div>
                    <div class="k">管理端地址</div><div class="v mono" id="ovAdminUrl">—</div>
                    <div class="k">哈希清单</div><div class="v" id="ovManifest">—</div>
                    <div class="k">服务端脚本版本</div><div class="v" id="ovServerVer">—</div>
                    <div class="k">运行时长</div><div class="v" id="ovUptime">—</div>
                    <div class="k">配置文件</div><div class="v mono" id="ovConfPath">—</div>
                </div>
            </div>

            <div class="card">
                <h2>操作</h2>
                <div class="btn-row">
                    <button class="btn btn-primary" id="reloadBtn">重新加载（重跑启动流程）</button>
                    <button class="btn" id="refreshBtn">刷新状态</button>
                    <button class="btn" id="gotoLogsBtn">查看运行日志</button>
                </div>
                <div class="tip" style="font-size:0.78rem;color:#999;margin-top:10px;">
                    重新加载 = 重读配置 → 检查目录 → 重跑文件重命名 → 重建哈希清单；不会中断玩家连接，也不会清除登录状态。
                </div>
                <div id="reloadSteps" class="steps hidden" style="margin-top:14px;border-top:1px solid #e4e4e4;padding-top:12px;"></div>
            </div>
        </section>

        <!-- 文件管理 -->
        <section id="page-files" class="page hidden">
            <h1>文件管理</h1>
            <div class="page-desc">上传、改名、删除分发文件；操作后哈希清单会自动重建</div>

            <div class="card">
                <h2>上传文件 <span class="hint" id="uploadHint">mods 仅接受 .jar，光影与材质仅接受 .zip</span></h2>
                <div class="drop" id="dropZone">
                    <div class="big">拖入文件到此处，或点击选择</div>
                    <div class="sub" id="dropHint">—</div>
                </div>
                <input type="file" id="fileInput" multiple class="hidden">
                <div class="queue" id="queue"></div>
            </div>

            <div class="card">
                <div class="tabs" id="kindTabs">
                    <button class="tab active" data-kind="mods">mods</button>
                    <button class="tab" data-kind="shaderpacks">shaderpacks</button>
                    <button class="tab" data-kind="resourcepacks">resourcepacks</button>
                </div>
                <div class="btn-row" style="margin-bottom:12px;justify-content:space-between;">
                    <input type="text" id="fileSearch" placeholder="搜索文件名…" style="max-width:280px;">
                    <span class="dim" id="fileCountText" style="font-size:0.82rem;">—</span>
                </div>
                <div style="overflow-x:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th style="width:44%;">文件名</th>
                                <th style="width:90px;">大小</th>
                                <th style="width:120px;">尾缀版本</th>
                                <th style="width:150px;">SHA1</th>
                                <th style="width:160px;">操作</th>
                            </tr>
                        </thead>
                        <tbody id="fileTableBody"><tr><td colspan="5" style="text-align:center;color:#999;padding:26px;">加载中…</td></tr></tbody>
                    </table>
                </div>
                <div class="btn-row" style="margin-top:14px;justify-content:space-between;">
                    <span class="dim" id="pageInfo" style="font-size:0.82rem;">—</span>
                    <span class="btn-row">
                        <button class="btn btn-sm" id="prevPageBtn">上一页</button>
                        <button class="btn btn-sm" id="nextPageBtn">下一页</button>
                    </span>
                </div>
            </div>
        </section>

        <!-- 版本与日志 -->
        <section id="page-version" class="page hidden">
            <h1>版本与更新日志</h1>
            <div class="page-desc">内容版本号决定客户端是否触发更新；Changelog 会直接显示在客户端更新界面</div>

            <div class="card">
                <h2>内容版本号</h2>
                <div class="field" style="max-width:320px;">
                    <label class="fl" for="verInput">版本号</label>
                    <input type="text" id="verInput" inputmode="numeric" maxlength="15" placeholder="例如 1.0.001">
                    <div class="tip">只允许 <span class="mono">数字.数字.数字</span> 三段格式（例如 <span class="mono">1.0.001</span>），不接受字母与其它符号，避免客户端解析异常。</div>
                </div>
                <div class="checkline">
                    <input type="checkbox" id="verRename" checked>
                    <label for="verRename">
                        保存后同步分发文件尾缀为 <span class="mono">_v&lt;新版本&gt;_ZAKO</span>
                        <div class="tip">与启动时的重命名行为一致；不勾选则只改版本号，文件尾缀保持不变（客户端仍按文件名含 _ZAKO 来管理文件）。</div>
                    </label>
                </div>
            </div>

            <div class="card">
                <h2>更新日志（Changelog.txt）</h2>
                <div class="field">
                    <textarea id="clInput" rows="8" placeholder="每行一条更新说明…"></textarea>
                    <div class="tip">客户端更新界面直接展示此内容；建议每行一条、不超过 20 行。<span class="dim" id="clCount">0 字符</span> · 保存后立即生效（无需重启）</div>
                </div>
            </div>

            <div class="btn-row">
                <button class="btn btn-primary" id="saveVersionBtn">保存版本与日志</button>
                <button class="btn" id="previewClBtn">预览客户端展示效果</button>
            </div>
        </section>

        <!-- 配置 -->
        <section id="page-config" class="page hidden">
            <h1>配置</h1>
            <div class="page-desc">修改后写入 speedupdate.conf（保留注释与顺序）；标 <span class="badge req">需重启</span> 的项要手动重启脚本才会生效</div>

            <div class="card">
                <h2>客户端连接</h2>
                <div class="row2">
                    <div class="field">
                        <label class="fl" for="cfg_port">服务端口 port <span class="badge req">需重启</span></label>
                        <input type="text" id="cfg_port" inputmode="numeric" maxlength="5">
                        <div class="tip">只允许 <b>1~65535</b> 的整数</div>
                    </div>
                    <div class="field">
                        <label class="fl" for="cfg_bind">监听地址 bind_v4 <span class="badge req">需重启</span></label>
                        <input type="text" id="cfg_bind" maxlength="15">
                        <div class="tip">必须是合法 IPv4，例如 <span class="mono">0.0.0.0</span> 或 <span class="mono">127.0.0.1</span>；管理端复用此地址</div>
                    </div>
                    <div class="field">
                        <label class="fl" for="cfg_v6">IPv6 监听 enable_v6 <span class="badge req">需重启</span></label>
                        <select id="cfg_v6"><option value="true">启用</option><option value="false">关闭</option></select>
                        <div class="tip">管理端同步遵循此开关</div>
                    </div>
                    <div class="field">
                        <label class="fl" for="cfg_maxdl">下载并发上限 max_downloads <span class="badge req">需重启</span></label>
                        <input type="text" id="cfg_maxdl" inputmode="numeric" maxlength="2">
                        <div class="tip">只允许 <b>1~64</b> 的整数（并发信号量在启动时创建，故需重启生效）</div>
                    </div>
                </div>
                <div class="field">
                    <label class="fl" for="cfg_feedback">反馈链接 feedback_url <span class="badge ok">即时生效</span></label>
                    <input type="text" id="cfg_feedback">
                    <div class="tip">客户端主界面「问题反馈」按钮跳转的地址，必须为 http/https 链接</div>
                </div>
            </div>

            <div class="card">
                <h2>管理端</h2>
                <div class="row2">
                    <div class="field">
                        <label class="fl" for="cfg_adminport">管理端口 admin_port <span class="badge req">需重启</span></label>
                        <input type="text" id="cfg_adminport" inputmode="numeric" maxlength="5">
                        <div class="tip">只允许 <b>0~65535</b> 的整数；填 0 = 下次重启后关闭管理端</div>
                    </div>
                    <div class="field">
                        <label class="fl" for="cfg_pwd">管理密码 admin_password <span class="badge ok">即时生效</span></label>
                        <input type="password" id="cfg_pwd" placeholder="留空表示不修改" autocomplete="new-password">
                        <div class="tip">明文存于配置文件；修改后所有登录状态立即失效，需要重新登录</div>
                    </div>
                </div>
            </div>

            <div class="btn-row">
                <button class="btn btn-primary" id="saveConfigBtn">保存配置</button>
                <button class="btn" id="reloadConfBtn">重新读取配置</button>
            </div>
        </section>

        <!-- 日志 -->
        <section id="page-logs" class="page hidden">
            <h1>日志</h1>
            <div class="page-desc">增量拉取（1.5 秒轮询），单次最多显示最近 300 行</div>

            <div class="card">
                <div class="tabs" id="logTabs">
                    <button class="tab active" data-log="run">运行日志 speedupdate.log</button>
                    <button class="tab" data-log="admin">审计日志 speedupdate_admin.log</button>
                </div>
                <div class="btn-row" style="margin-bottom:12px;justify-content:space-between;">
                    <input type="text" id="logSearch" placeholder="关键字过滤…" style="max-width:260px;">
                    <span class="btn-row">
                        <label class="checkline" style="gap:6px;"><input type="checkbox" id="autoScroll" checked> <span>自动滚底</span></label>
                        <button class="btn btn-sm" id="pauseBtn">暂停实时</button>
                        <button class="btn btn-sm" id="clearLogBtn">清屏</button>
                        <button class="btn btn-sm" id="exportLogBtn">导出</button>
                    </span>
                </div>
                <div class="logbox" id="logBox"></div>
                <div class="tip" style="font-size:0.78rem;color:#999;margin-top:8px;">
                    已接收 <span id="logTotal">0</span> 行 · 显示 <span id="logShown">0</span> 行 · <span id="logState">实时追加中</span>
                </div>
            </div>
        </section>
    </div>
</div>

<div id="modalMask" class="mask hidden">
    <div class="modal">
        <h3 id="modalTitle">标题</h3>
        <div class="mbody" id="modalBody"></div>
        <div class="modal-foot">
            <button class="btn" id="modalCancel">取消</button>
            <button class="btn btn-primary" id="modalOk">确定</button>
        </div>
    </div>
</div>

<div id="toast" class="toast"></div>

<script>
'use strict';

/* ================= 工具 ================= */
const $ = function (s) { return document.querySelector(s); };
const $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}
function fmtSize(b) {
    b = Number(b) || 0;
    if (b < 1024) return b + ' B';
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB';
    return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}
function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + ' 天 ' + h + ' 小时';
    if (h > 0) return h + ' 小时 ' + m + ' 分';
    return m + ' 分 ' + (sec % 60) + ' 秒';
}
function pad2(n) { return n < 10 ? '0' + n : '' + n; }
function timeStr(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}
let toastTimer = null;
function toast(msg, type) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || 'success');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = 'toast ' + (type || 'success'); }, 3000);
}

/* ================= 模态框 ================= */
function openModal(opt) {
    $('#modalTitle').textContent = opt.title || '';
    $('#modalBody').innerHTML = opt.body || '';
    $('#modalCancel').textContent = opt.cancelText || '取消';
    const ok = $('#modalOk');
    ok.textContent = opt.okText || '确定';
    ok.className = 'btn ' + (opt.danger ? 'btn-danger' : 'btn-primary');
    ok.onclick = function () { closeModal(); if (opt.onOk) opt.onOk(); };
    $('#modalCancel').style.display = opt.hideCancel ? 'none' : '';
    $('#modalMask').classList.remove('hidden');
    if (opt.afterRender) setTimeout(opt.afterRender, 30);
}
function closeModal() { $('#modalMask').classList.add('hidden'); }
$('#modalCancel').onclick = closeModal;
$('#modalMask').onclick = function (e) { if (e.target === $('#modalMask')) closeModal(); };

/* ================= API ================= */
const API = {
    request: function (path, opts) {
        const o = opts || {};
        o.credentials = 'same-origin';
        if (o.json) {
            o.method = o.method || 'POST';
            o.headers = { 'Content-Type': 'application/json' };
            o.body = JSON.stringify(o.json);
            delete o.json;
        }
        return fetch(path, o).then(function (r) {
            return r.text().then(function (txt) {
                let data = {};
                if (txt) { try { data = JSON.parse(txt); } catch (e) { data = { error: '服务端返回了非 JSON 数据' }; } }
                if (r.status === 401) {
                    if ($('#app') && !$('#app').classList.contains('hidden')) {
                        showLogin();
                        toast('登录状态已失效，请重新登录', 'warn');
                    }
                    throw { code: 401, error: data.error || '未登录' };
                }
                if (!r.ok) throw { code: r.status, error: data.error || ('请求失败（HTTP ' + r.status + '）') };
                return data;
            });
        }).catch(function (err) {
            if (err && err.code) throw err;
            throw { code: 0, error: '网络错误或服务端未响应，请检查服务端是否在运行' };
        });
    },
    get: function (p) { return API.request(p); },
    post: function (p, json) { return API.request(p, { json: json || {} }); }
};

/* ================= 状态 ================= */
const state = {
    page: 'overview',
    kind: 'mods',
    logTab: 'run',
    fileSearch: '',
    logSearch: '',
    filePage: 1,
    pageSize: 16,
    autoScroll: true,
    streaming: true,
    logged: false,
    files: [],
    total: 0,
    queue: [],
    logOffset: 0,
    logLines: [],
    reloadTimer: null,
    logTimer: null,
    stateTimer: null,
    rebuildTimer: null
};

/* ================= 登录 ================= */
function showLogin() {
    state.logged = false;
    stopTimers();
    $('#app').classList.add('hidden');
    $('#loginPage').classList.remove('hidden');
    $('#loginPwd').value = '';
    setTimeout(function () { $('#loginPwd').focus(); }, 50);
}
function showApp() {
    state.logged = true;
    $('#loginPage').classList.add('hidden');
    $('#app').classList.remove('hidden');
    go(state.page);
    startStateTimer();
}
function stopTimers() {
    if (state.reloadTimer) { clearInterval(state.reloadTimer); state.reloadTimer = null; }
    if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
    if (state.stateTimer) { clearInterval(state.stateTimer); state.stateTimer = null; }
    if (state.rebuildTimer) { clearInterval(state.rebuildTimer); state.rebuildTimer = null; }
}
function startStateTimer() {
    if (state.stateTimer) clearInterval(state.stateTimer);
    state.stateTimer = setInterval(function () {
        if (state.page === 'overview') loadState(true);
    }, 10000);
}
function doLogin() {
    const pwd = $('#loginPwd').value;
    if (!pwd) { toast('请输入管理密码', 'error'); return; }
    const btn = $('#loginBtn');
    btn.disabled = true;
    btn.textContent = '登录中…';
    API.post('/api/login', { password: pwd }).then(function () {
        btn.disabled = false;
        btn.textContent = '登 录';
        showApp();
        toast('登录成功');
    }, function (e) {
        btn.disabled = false;
        btn.textContent = '登 录';
        toast(e.error || '登录失败', 'error');
        $('#loginPwd').value = '';
        $('#loginPwd').focus();
    });
}
$('#loginBtn').onclick = doLogin;
$('#loginPwd').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
$('#forgotBtn').onclick = function () {
    openModal({
        title: '忘记密码',
        body: '<p>管理密码以明文保存在服务端配置文件中，请直接修改：</p>' +
            '<div class="box mono">speedupdate.conf → admin_password=你的新密码</div>' +
            '<p>保存后手动重启服务端脚本即可生效。</p>' +
            '<p class="dim" style="font-size:0.82rem;">出于安全考虑，管理端不提供网页端找回或重置密码的入口。</p>',
        okText: '我知道了',
        hideCancel: true
    });
};
$('#logoutBtn').onclick = function () {
    openModal({
        title: '退出登录',
        body: '<p>将清除服务端会话与浏览器凭证，下次访问需要重新输入密码。</p>',
        okText: '退出',
        onOk: function () {
            API.post('/api/logout', {}).then(function () {
                showLogin();
                toast('已退出登录');
            }, function () {
                showLogin();
                toast('已退出登录');
            });
        }
    });
};

/* ================= 路由 ================= */
const PAGE_TITLES = { overview: '概览', files: '文件管理', version: '版本与日志', config: '配置', logs: '运行日志' };
function go(page) {
    state.page = page;
    $$('.page').forEach(function (el) { el.classList.add('hidden'); });
    const target = $('#page-' + page);
    if (target) target.classList.remove('hidden');
    $$('.topbar .nav a[data-nav]').forEach(function (a) { a.classList.toggle('active', a.dataset.nav === page); });
    document.title = '云端速递 · 管理端 - ' + (PAGE_TITLES[page] || '');
    if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
    if (page === 'overview') loadState(false);
    if (page === 'files') loadFiles();
    if (page === 'version') loadVersion();
    if (page === 'config') loadConfig();
    if (page === 'logs') { state.logOffset = 0; state.logLines = []; pollLogs(); state.logTimer = setInterval(pollLogs, 1500); }
}
$$('.topbar .nav a[data-nav]').forEach(function (a) { a.onclick = function () { go(a.dataset.nav); }; });
$('#gotoLogsBtn').onclick = function () { go('logs'); };

/* ================= 概览 ================= */
function adminOrigin() {
    const o = location.origin;
    if (!o || o === 'null' || o === 'file://') return '—';
    return o;
}
function renderRebuildNotice(on) {
    $('#rebuildNotice').classList.toggle('hidden', !on);
}
function loadState(silent) {
    return API.get('/api/state').then(function (d) {
        $('#ovVersion').textContent = d.version;
        $('#ovFileCount').innerHTML = d.files.total + ' <small>个</small>';
        $('#ovFileSize').textContent = 'mods ' + d.files.mods + ' · 光影 ' + d.files.shaderpacks + ' · 材质 ' + d.files.resourcepacks +
            ' · 合计 ' + fmtSize(d.files.bytes);
        $('#ovClientIps').innerHTML = d.todayIps + ' <small>个 IP</small>';
        $('#ovClientConns').textContent = '今日累计连接 ' + d.todayConns + ' 次 · 同一 IP 只计一次';
        $('#ovDownload').innerHTML = d.activeDownloads + ' <small>/ ' + d.maxDownloads + '</small>';
        $('#ovListen').textContent = 'TCP ' + d.port + '（IPv4 ' + d.bindV4 + (d.enableV6 ? ' · IPv6 已启用' : ' · IPv6 已关闭') + '）';
        $('#ovAdminUrl').innerHTML = esc(adminOrigin()) + ' <span class="dim">（以当前访问地址为准）</span>';
        $('#ovManifest').textContent = d.manifestCount + ' 个文件 · 最后重建 ' + (d.lastRebuild || '—') +
            (d.lastRebuildSec ? '（耗时 ' + d.lastRebuildSec.toFixed(1) + ' 秒）' : '');
        $('#ovServerVer').innerHTML = 'v' + esc(d.serverVersion) + ' <span class="dim">（与整合包内容版本是两套体系）</span>';
        $('#ovUptime').textContent = fmtDuration(d.uptimeSec);
        $('#ovConfPath').textContent = d.confPath;
        renderRebuildNotice(!!d.rebuilding);
        if (!silent) { /* 静默刷新不提示 */ }
    }, function (e) {
        if (e.code !== 401) toast(e.error, 'error');
    });
}
$('#refreshBtn').onclick = function () {
    loadState(true).then(function () { toast('状态已刷新'); }, function () {});
};
function renderReloadSteps(steps) {
    const box = $('#reloadSteps');
    box.classList.remove('hidden');
    box.innerHTML = steps.map(function (s, i) {
        const cls = s.state === 'done' ? 'done' : (s.state === 'doing' ? 'doing' : (s.state === 'fail' ? 'fail' : ''));
        const mark = s.state === 'done' ? '✓' : (s.state === 'fail' ? '×' : (i + 1));
        return '<div class="step ' + cls + '"><span class="dot">' + mark + '</span><span>' + esc(s.name) + '</span></div>';
    }).join('');
}
$('#reloadBtn').onclick = function () {
    const btn = $('#reloadBtn');
    btn.disabled = true;
    API.post('/api/reload', {}).then(function () {
        toast('已开始重新加载…', 'warn');
        if (state.reloadTimer) clearInterval(state.reloadTimer);
        state.reloadTimer = setInterval(function () {
            API.get('/api/reload/status').then(function (d) {
                if (d.steps && d.steps.length) renderReloadSteps(d.steps);
                if (!d.running) {
                    clearInterval(state.reloadTimer);
                    state.reloadTimer = null;
                    btn.disabled = false;
                    if (d.error) {
                        toast('重新加载失败：' + d.error, 'error');
                    } else if (d.result) {
                        toast('重新加载完成：清单 ' + d.result.manifestCount + ' 个文件，耗时 ' + d.result.elapsedSec.toFixed(2) + ' 秒' +
                            (d.result.renamed === undefined ? '' : '（改名 ' + d.result.renamed + '，跳过 ' + d.result.skipped + '，失败 ' + d.result.failed + '）'));
                        loadState(true);
                    } else {
                        toast('重新加载已结束', 'warn');
                    }
                }
            }, function () {
                clearInterval(state.reloadTimer);
                state.reloadTimer = null;
                btn.disabled = false;
            });
        }, 700);
    }, function (e) {
        btn.disabled = false;
        toast(e.error, 'error');
    });
};

/* ================= 文件管理 ================= */
function kindLabel(kind) { return kind === 'mods' ? 'mods（.jar）' : (kind === 'shaderpacks' ? 'shaderpacks（.zip）' : 'resourcepacks（.zip）'); }
function suffixOf(name) {
    const m = String(name).match(/_v(\d+\.\d+\.\d+)_ZAKO\.[A-Za-z0-9]+$/i);
    return m ? m[1] : '';
}
function isManaged(name) { return /_ZAKO\.[A-Za-z0-9]+$/i.test(String(name)); }

function loadFiles() {
    const kind = state.kind;
    $('#dropHint').textContent = '当前目标：' + kindLabel(kind);
    $$('#kindTabs .tab').forEach(function (t) { t.classList.toggle('active', t.dataset.kind === kind); });
    $('#fileTableBody').innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;padding:26px;">加载中…</td></tr>';
    return API.get('/api/files?kind=' + encodeURIComponent(kind)).then(function (d) {
        if (kind !== state.kind) return;
        state.files = d.files || [];
        state.total = d.total || 0;
        renderFiles();
    }, function (e) {
        $('#fileTableBody').innerHTML = '<tr><td colspan="5" style="text-align:center;color:#c0392b;padding:26px;">' + esc(e.error) + '</td></tr>';
    });
}
function renderFiles() {
    const kw = state.fileSearch.trim().toLowerCase();
    const list = state.files.filter(function (f) { return !kw || f.name.toLowerCase().indexOf(kw) >= 0; });
    const pages = Math.max(1, Math.ceil(list.length / state.pageSize));
    if (state.filePage > pages) state.filePage = pages;
    const start = (state.filePage - 1) * state.pageSize;
    const view = list.slice(start, start + state.pageSize);
    const body = $('#fileTableBody');
    if (!view.length) {
        body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;padding:26px;">' +
            (state.total ? '没有匹配的文件' : '该目录暂无文件') + '</td></tr>';
    } else {
        body.innerHTML = view.map(function (f) {
            const sfx = suffixOf(f.name);
            return '<tr>' +
                '<td class="fname">' + esc(f.name) +
                (isManaged(f.name) ? '' : ' <span class="badge dim" title="文件名不含 _ZAKO，客户端不会管理此文件">不受客户端管理</span>') +
                '</td>' +
                '<td class="mono">' + fmtSize(f.size) + '</td>' +
                '<td>' + (sfx ? '<span class="badge ok">_v' + esc(sfx) + '_ZAKO</span>' : '<span class="badge dim">无</span>') + '</td>' +
                '<td class="mono" title="' + esc(f.sha) + '">' + esc(String(f.sha).slice(0, 10)) + '… ' +
                '<button class="btn btn-sm" data-act="copy" data-sha="' + esc(f.sha) + '">复制</button></td>' +
                '<td><span class="ops">' +
                '<button class="btn btn-sm" data-act="rename" data-name="' + esc(f.name) + '">改名</button>' +
                '<button class="btn btn-sm" data-act="download" data-name="' + esc(f.name) + '">下载</button>' +
                '<button class="btn btn-sm btn-danger" data-act="delete" data-name="' + esc(f.name) + '">删除</button>' +
                '</span></td></tr>';
        }).join('');
    }
    $('#fileCountText').textContent = '共 ' + state.total + ' 个文件' + (kw ? '（匹配 ' + list.length + ' 条）' : '');
    $('#pageInfo').textContent = '第 ' + state.filePage + ' / ' + pages + ' 页（每页 ' + state.pageSize + ' 条）';
    $('#prevPageBtn').disabled = state.filePage <= 1;
    $('#nextPageBtn').disabled = state.filePage >= pages;
}
$('#prevPageBtn').onclick = function () { if (state.filePage > 1) { state.filePage--; renderFiles(); } };
$('#nextPageBtn').onclick = function () { state.filePage++; renderFiles(); };
$$('#kindTabs .tab').forEach(function (t) {
    t.onclick = function () {
        state.kind = t.dataset.kind;
        state.fileSearch = '';
        state.filePage = 1;
        $('#fileSearch').value = '';
        loadFiles();
    };
});
$('#fileSearch').oninput = function () { state.fileSearch = this.value; state.filePage = 1; renderFiles(); };

$('#fileTableBody').addEventListener('click', function (e) {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'copy') {
        const sha = btn.dataset.sha;
        if (navigator.clipboard) {
            navigator.clipboard.writeText(sha).then(function () { toast('SHA1 已复制'); }, function () { toast('复制失败，请手动选择', 'error'); });
        } else { toast('浏览器不支持自动复制', 'warn'); }
        return;
    }
    const name = btn.dataset.name;
    if (act === 'rename') askRename(name);
    if (act === 'delete') askDelete(name);
    if (act === 'download') {
        const url = '/api/file/download?kind=' + encodeURIComponent(state.kind) + '&name=' + encodeURIComponent(name);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast('已开始下载：' + name);
    }
});

/* ---- 上传 ---- */
const dropZone = $('#dropZone');
const fileInput = $('#fileInput');
dropZone.onclick = function () { fileInput.click(); };
fileInput.onchange = function () { handleFiles(this.files); this.value = ''; };
['dragenter', 'dragover'].forEach(function (ev) {
    dropZone.addEventListener(ev, function (e) { e.preventDefault(); dropZone.classList.add('over'); });
});
['dragleave', 'drop'].forEach(function (ev) {
    dropZone.addEventListener(ev, function (e) { e.preventDefault(); dropZone.classList.remove('over'); });
});
dropZone.addEventListener('drop', function (e) { if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files); });

const WIN_RESERVED = ['CON', 'PRN', 'AUX', 'NUL'];
for (let ri = 1; ri <= 9; ri++) { WIN_RESERVED.push('COM' + ri); WIN_RESERVED.push('LPT' + ri); }
const MAX_UPLOAD = 4 * 1024 * 1024 * 1024;
function validateLocalName(name, kind) {
    if (!name) return '文件名不能为空';
    if (name.length > 200) return '文件名过长（最多 200 字符）';
    if (/[\\/:*?"<>|\u0000-\u001f\u202a-\u202e]/.test(name)) return '文件名不能包含 \\ / : * ? " < > | 等特殊字符';
    if (name.startsWith('.')) return '文件名不能以点开头';
    if (name.endsWith('.') || name.endsWith(' ')) return '文件名不能以点或空格结尾';
    const dot = name.lastIndexOf('.');
    const stem = (dot > 0 ? name.slice(0, dot) : name).trim().toUpperCase();
    if (WIN_RESERVED.indexOf(stem) >= 0) return '文件名不合法（Windows 保留名）';
    const ext = kind === 'mods' ? '.jar' : '.zip';
    if (!name.toLowerCase().endsWith(ext)) return kind + ' 目录只接受 ' + ext + ' 文件';
    return '';
}
function handleFiles(fileList) {
    Array.prototype.slice.call(fileList).forEach(function (f) {
        const err = validateLocalName(f.name, state.kind);
        if (err) { toast(f.name + '：' + err, 'error'); return; }
        if (f.size > MAX_UPLOAD) { toast(f.name + '：文件超过 4 GB 上限，无法上传', 'error'); return; }
        const exist = state.files.filter(function (x) { return x.name === f.name; })[0];
        if (exist) askOverwrite(f, exist);
        else startUpload(f, false);
    });
}
function askOverwrite(file, exist) {
    const same = Number(file.size) === Number(exist.size);
    openModal({
        title: '云端已存在同名文件',
        body: '<p>目标目录 <span class="mono">' + esc(state.kind) + '/</span> 中已存在同名文件：</p>' +
            '<div class="box"><div class="mono">' + esc(exist.name) + '</div>' +
            '<div class="dim" style="font-size:0.82rem;margin-top:4px;">现有 ' + fmtSize(exist.size) +
            ' ／ 新上传 ' + fmtSize(file.size) + (same ? '（大小相同）' : '') + '</div></div>' +
            '<p>替换后旧文件被覆盖，哈希清单会立即重建，客户端在下次检查时同步更新。</p>',
        okText: '替换',
        danger: true,
        onOk: function () { startUpload(file, true); }
    });
}
function startUpload(file, overwrite) {
    const item = { name: file.name, size: file.size, progress: 0, status: '上传中', failed: false };
    state.queue.push(item);
    renderQueue();
    const kind = state.kind;
    const url = '/api/upload?kind=' + encodeURIComponent(kind) + '&name=' + encodeURIComponent(file.name) + '&overwrite=' + (overwrite ? 1 : 0);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) { item.progress = e.loaded / e.total * 100; renderQueue(); }
    };
    xhr.onload = function () {
        let d = {};
        try { d = JSON.parse(xhr.responseText || '{}'); } catch (e2) { d = {}; }
        if (xhr.status >= 200 && xhr.status < 300) {
            item.progress = 100;
            item.status = '完成';
            renderQueue();
            if (state.kind === kind) { loadFiles(); }
            toast('上传完成：' + file.name + '（清单正在后台重建）');
            watchRebuild();
            setTimeout(function () { state.queue = state.queue.filter(function (x) { return x !== item; }); renderQueue(); }, 1500);
        } else if (xhr.status === 401) {
            item.status = '登录失效'; item.failed = true; renderQueue();
            showLogin();
            toast('登录状态已失效，请重新登录', 'warn');
        } else if (xhr.status === 409) {
            item.status = '云端已存在'; item.progress = 0; renderQueue();
            API.get('/api/files?kind=' + encodeURIComponent(kind)).then(function (dd) {
                const ex = (dd.files || []).filter(function (x) { return x.name === file.name; })[0] || { name: file.name, size: 0 };
                askOverwrite(file, ex);
            }, function () { toast('云端已存在同名文件', 'error'); });
        } else if (xhr.status === 413) {
            item.status = '文件过大'; item.failed = true; renderQueue();
            toast(file.name + '：超过服务端 4 GB 上限，已拒绝', 'error');
        } else {
            item.failed = true;
            renderQueue();
            toast(file.name + '：' + (d.error || '上传失败'), 'error');
        }
    };
    xhr.onerror = function () {
        item.status = '网络中断';
        item.failed = true;
        renderQueue();
        toast(file.name + '：网络中断，上传失败', 'error');
    };
    xhr.send(file);
}
function renderQueue() {
    const box = $('#queue');
    if (!state.queue.length) { box.innerHTML = ''; return; }
    box.innerHTML = state.queue.map(function (q) {
        const pct = Math.min(100, Math.round(q.progress));
        return '<div class="qitem">' +
            '<div class="fname">' + esc(q.name) + '</div>' +
            '<div class="mono">' + fmtSize(q.size) + '</div>' +
            '<div class="bar"><i class="' + (q.failed ? 'err' : '') + '" style="width:' + pct + '%"></i></div>' +
            '<div class="dim" style="font-size:0.8rem;">' + esc(q.status) + '</div>' +
            '</div>';
    }).join('');
}
/* 操作后等待后台清单重建完成再刷新列表 */
function watchRebuild() {
    if (state.rebuildTimer) clearInterval(state.rebuildTimer);
    let ticks = 0;
    renderRebuildNotice(true);
    state.rebuildTimer = setInterval(function () {
        ticks++;
        API.get('/api/state').then(function (d) {
            if (!d.rebuilding || ticks > 60) {
                clearInterval(state.rebuildTimer);
                state.rebuildTimer = null;
                renderRebuildNotice(false);
                if (state.page === 'files') loadFiles();
                if (state.page === 'overview') loadState(true);
            }
        }, function () {
            clearInterval(state.rebuildTimer);
            state.rebuildTimer = null;
            renderRebuildNotice(false);
        });
    }, 1200);
}

/* ---- 改名 ---- */
function askRename(name) {
    const kind = state.kind;
    openModal({
        title: '重命名文件',
        body: '<div class="field"><label class="fl">当前名称</label><input type="text" value="' + esc(name) + '" readonly></div>' +
            '<div class="field"><label class="fl">新名称</label><input type="text" id="renameInput" value="' + esc(name) + '" autocomplete="off">' +
            '<div class="tip">仅在同一目录内改名，不移动位置。</div></div>' +
            '<div class="tip" style="color:#a26a00;">注意：文件名必须保留 <b>_v&lt;版本&gt;_ZAKO</b> 尾缀，否则客户端将不再管理此文件（既不下发也不删除）。</div>',
        okText: '确认改名',
        afterRender: function () {
            const el = $('#renameInput');
            if (!el) return;
            el.focus();
            const dot = name.lastIndexOf('.');
            el.setSelectionRange(0, dot > 0 ? dot : name.length);
        },
        onOk: function () {
            const v = $('#renameInput').value.trim();
            if (v === name) { toast('名称未变化', 'warn'); return; }
            const err = validateLocalName(v, kind);
            if (err) { toast(err, 'error'); return; }
            API.post('/api/file/rename', { kind: kind, name: name, newName: v }).then(function (d) {
                if (d.warning) {
                    openModal({
                        title: '文件已脱离客户端管理',
                        body: '<p>' + esc(d.warning) + '</p><div class="box mono">' + esc(v) + '</div>' +
                            '<p>玩家本地对应的旧文件不会被删除，也不会再收到此文件的更新。</p>',
                        okText: '我知道了',
                        hideCancel: true
                    });
                } else {
                    toast('已改名，清单正在后台重建');
                }
                if (state.kind === kind) loadFiles();
                watchRebuild();
            }, function (e) { toast(e.error, 'error'); });
        }
    });
}

/* ---- 删除 ---- */
function askDelete(name) {
    const kind = state.kind;
    const rec = state.files.filter(function (x) { return x.name === name; })[0];
    openModal({
        title: '确认删除文件',
        body: '<p>即将从服务器永久删除以下文件：</p>' +
            '<div class="box"><div class="mono">' + esc(name) + '</div>' +
            '<div class="dim" style="font-size:0.82rem;margin-top:4px;">' + fmtSize(rec ? rec.size : 0) + ' · ' + esc(kind) + '/</div></div>' +
            '<p class="warn-text">删除不可恢复。玩家本地已下载的文件不会被自动清理，但该文件将不再出现在更新清单中。</p>' +
            '<p class="dim" style="font-size:0.82rem;">若该文件正在被玩家下载，删除会因文件占用而失败，稍后重试即可。</p>',
        okText: '永久删除',
        danger: true,
        onOk: function () {
            API.post('/api/file/delete', { kind: kind, name: name }).then(function () {
                toast('已删除：' + name);
                if (state.kind === kind) loadFiles();
                watchRebuild();
            }, function (e) { toast(e.error, 'error'); });
        }
    });
}

/* ================= 版本与日志 ================= */
function loadVersion() {
    return API.get('/api/version').then(function (d) {
        $('#verInput').value = d.version;
        $('#clInput').value = d.changelog || '';
        $('#clCount').textContent = $('#clInput').value.length + ' 字符';
    }, function (e) { toast(e.error, 'error'); });
}
$('#clInput').oninput = function () { $('#clCount').textContent = this.value.length + ' 字符'; };
$('#verInput').addEventListener('input', function () {
    let v = this.value.replace(/[^0-9.]/g, '').replace(/\.{2,}/g, '.');
    const parts = v.split('.');
    if (parts.length > 3) v = parts.slice(0, 3).join('.');
    if (v !== this.value) this.value = v;
});
$('#saveVersionBtn').onclick = function () {
    const v = $('#verInput').value.trim();
    if (!/^\d+\.\d+\.\d+$/.test(v)) { toast('版本号必须为 数字.数字.数字 三段格式，例如 1.0.001', 'error'); return; }
    const rename = $('#verRename').checked;
    const changelog = $('#clInput').value;
    const btn = $('#saveVersionBtn');
    btn.disabled = true;
    API.post('/api/version', { version: v, changelog: changelog, rename: rename }).then(function (d) {
        btn.disabled = false;
        if (d.warning) {
            toast(d.warning, 'warn');
            loadVersion();
        } else if (d.background) {
            toast('版本号已保存，正在后台同步文件尾缀并重建清单…', 'warn');
            watchRebuild();
        } else {
            toast('版本号与更新日志已保存：' + v + '（文件尾缀未改动）');
        }
        loadState(true);
    }, function (e) {
        btn.disabled = false;
        toast(e.error, 'error');
    });
};
$('#previewClBtn').onclick = function () {
    const lines = $('#clInput').value.split('\n').filter(function (x) { return x.trim(); });
    openModal({
        title: '客户端更新界面预览',
        body: '<div class="box" style="font-size:0.86rem;">' +
            '<div style="margin-bottom:8px;"><b>发现新版本 ' + esc($('#verInput').value) + '</b></div>' +
            (lines.length ? lines.map(function (l) { return '<div style="color:#555;">' + esc(l) + '</div>'; }).join('') : '<div class="dim">（更新日志为空）</div>') +
            '</div><p class="dim" style="font-size:0.8rem;">内容来自 Changelog.txt，客户端只做只读展示。</p>',
        okText: '关闭',
        hideCancel: true
    });
};

/* ================= 配置 ================= */
let cfgOrig = {};
function loadConfig() {
    return API.get('/api/config').then(function (d) {
        const c = d.config;
        $('#cfg_port').value = c.port;
        $('#cfg_bind').value = c.bind_v4;
        $('#cfg_v6').value = c.enable_v6 ? 'true' : 'false';
        $('#cfg_maxdl').value = c.max_downloads;
        $('#cfg_feedback').value = c.feedback_url;
        $('#cfg_adminport').value = c.admin_port;
        $('#cfg_pwd').value = '';
        cfgOrig = {
            port: String(c.port), bind_v4: String(c.bind_v4), enable_v6: c.enable_v6 ? 'true' : 'false',
            max_downloads: String(c.max_downloads), feedback_url: String(c.feedback_url), admin_port: String(c.admin_port)
        };
    }, function (e) { toast(e.error, 'error'); });
}
$('#reloadConfBtn').onclick = function () { loadConfig().then(function () { toast('已重新读取配置文件'); }, function () {}); };
$$('#page-config input[inputmode="numeric"]').forEach(function (el) {
    el.addEventListener('input', function () {
        const v = this.value.replace(/[^0-9]/g, '');
        if (v !== this.value) this.value = v;
    });
});
function isIPv4(s) {
    const p = String(s).split('.');
    if (p.length !== 4) return false;
    return p.every(function (x) { return /^\d{1,3}$/.test(x) && Number(x) >= 0 && Number(x) <= 255; });
}
$('#saveConfigBtn').onclick = function () {
    const port = $('#cfg_port').value.trim();
    const bind = $('#cfg_bind').value.trim();
    const v6 = $('#cfg_v6').value;
    const maxdl = $('#cfg_maxdl').value.trim();
    const feedback = $('#cfg_feedback').value.trim();
    const adminPort = $('#cfg_adminport').value.trim();
    const pwd = $('#cfg_pwd').value;
    let bad = null;
    if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) bad = { el: '#cfg_port', msg: '服务端口必须是 1~65535 的整数' };
    else if (!isIPv4(bind)) bad = { el: '#cfg_bind', msg: '监听地址必须是合法 IPv4，例如 0.0.0.0 或 127.0.0.1' };
    else if (!/^\d+$/.test(maxdl) || Number(maxdl) < 1 || Number(maxdl) > 64) bad = { el: '#cfg_maxdl', msg: '下载并发上限必须是 1~64 的整数' };
    else if (!/^\d+$/.test(adminPort) || Number(adminPort) < 0 || Number(adminPort) > 65535) bad = { el: '#cfg_adminport', msg: '管理端口必须是 0~65535 的整数（0 = 关闭管理端）' };
    else if (!/^https?:\/\/.+/.test(feedback)) bad = { el: '#cfg_feedback', msg: '反馈链接必须以 http:// 或 https:// 开头' };
    if (bad) {
        toast(bad.msg, 'error');
        const el = $(bad.el);
        if (el) {
            el.focus();
            el.style.borderColor = '#e74c3c';
            setTimeout(function () { el.style.borderColor = ''; }, 1800);
        }
        return;
    }
    const payload = {
        port: Number(port), bind_v4: bind, enable_v6: v6 === 'true', max_downloads: Number(maxdl),
        feedback_url: feedback, admin_port: Number(adminPort)
    };
    if (pwd) payload.admin_password = pwd;
    const btn = $('#saveConfigBtn');
    btn.disabled = true;
    API.post('/api/config', payload).then(function (d) {
        btn.disabled = false;
        if (d.restartKeys && d.restartKeys.length) {
            openModal({
                title: '部分改动需手动重启才生效',
                body: '<p>以下配置已写入 <span class="mono">speedupdate.conf</span>，但需要手动重启服务端脚本后才会生效：</p>' +
                    '<div class="box mono">' + d.restartKeys.join('、') + '</div>' +
                    '<p class="dim" style="font-size:0.82rem;">其余配置项已即时生效。</p>',
                okText: '我知道了',
                hideCancel: true
            });
        } else {
            toast('配置已保存并即时生效');
        }
        loadConfig();
        if (d.passwordChanged) {
            openModal({
                title: '管理密码已修改',
                body: '<p>密码已写入配置文件。为保证安全，<b>所有登录状态已立即失效</b>，需要重新登录。</p>' +
                    '<p class="dim" style="font-size:0.82rem;">（其它正在使用管理端的浏览器也会被登出）</p>',
                okText: '重新登录',
                hideCancel: true,
                onOk: function () { showLogin(); toast('请使用新密码登录', 'warn'); }
            });
        }
    }, function (e) {
        btn.disabled = false;
        toast(e.error, 'error');
    });
};

/* ================= 日志 ================= */
function logLevelOf(line) {
    if (/\[ERROR/.test(line)) return 'e';
    if (/\[WARN/.test(line)) return 'w';
    return 'i';
}
$$('#logTabs .tab').forEach(function (t) {
    t.onclick = function () {
        state.logTab = t.dataset.log;
        $$('#logTabs .tab').forEach(function (x) { x.classList.toggle('active', x === t); });
        state.logOffset = 0;
        state.logLines = [];
        pollLogs();
    };
});
$('#logSearch').oninput = function () { state.logSearch = this.value; renderLogs(); };
$('#autoScroll').onchange = function () { state.autoScroll = this.checked; if (this.checked) scrollLogBottom(); };
$('#pauseBtn').onclick = function () {
    state.streaming = !state.streaming;
    this.textContent = state.streaming ? '暂停实时' : '继续实时';
    $('#logState').textContent = state.streaming ? '实时追加中' : '已暂停';
    toast(state.streaming ? '已继续实时刷新' : '已暂停实时刷新', 'warn');
};
$('#clearLogBtn').onclick = function () {
    state.logLines = [];
    renderLogs();
    toast('已清屏（不影响服务端日志文件）');
};
$('#exportLogBtn').onclick = function () {
    const text = state.logLines.map(function (x) { return x.text; }).join('\r\n');
    const name = state.logTab === 'run' ? 'speedupdate.log' : 'speedupdate_admin.log';
    const blob = new Blob(['\ufeff' + text], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1500);
    toast('已导出 ' + state.logLines.length + ' 行到 ' + name);
};
function pollLogs() {
    if (!state.streaming) return;
    const which = state.logTab;
    const offset = state.logOffset;
    API.get('/api/logs?which=' + which + '&offset=' + offset).then(function (d) {
        if (which !== state.logTab) return;
        if (d.reset) { state.logLines = []; }
        (d.lines || []).forEach(function (line) { state.logLines.push({ text: line, lv: logLevelOf(line) }); });
        if (state.logLines.length > 3000) state.logLines = state.logLines.slice(-3000);
        state.logOffset = d.offset;
        renderLogs();
    }, function (e) { if (e.code !== 401) $('#logState').textContent = '拉取失败：' + e.error; });
}
function renderLogs() {
    const kw = state.logSearch.trim().toLowerCase();
    const filtered = kw ? state.logLines.filter(function (x) { return x.text.toLowerCase().indexOf(kw) >= 0; }) : state.logLines;
    const shown = filtered.slice(-300);
    const box = $('#logBox');
    box.innerHTML = shown.map(function (x) {
        let t = esc(x.text);
        if (kw) {
            const idx = t.toLowerCase().indexOf(kw);
            if (idx >= 0) t = t.slice(0, idx) + '<span class="hi">' + t.slice(idx, idx + kw.length) + '</span>' + t.slice(idx + kw.length);
        }
        return '<div class="lv-' + x.lv + '">' + t + '</div>';
    }).join('');
    $('#logTotal').textContent = state.logLines.length;
    $('#logShown').textContent = shown.length;
    if (state.autoScroll) scrollLogBottom();
}
function scrollLogBottom() {
    const box = $('#logBox');
    box.scrollTop = box.scrollHeight;
}

/* ================= 启动 ================= */
(function init() {
    if (location.protocol === 'file:') {
        $('#loginPage').classList.add('hidden');
        $('#fileNotice').classList.remove('hidden');
        return;
    }
    /* 用一次状态查询判断当前会话是否仍然有效 */
    API.get('/api/state').then(function () {
        showApp();
    }, function () {
        showLogin();
    });
})();
</script>
</body>
</html>
'''

# 版本号合法性：只允许「数字.数字.数字」三段（与客户端解析保持一致，避免字母等引发异常）
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# ========== 控制台编码与彩色输出 ==========
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"
C_BLUE = "\033[94m"


def _enable_ansi_windows():
    """Windows 控制台默认不开启 ANSI 转义，需要打开 VT 处理才能显示颜色。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE / STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if not handle:
                continue
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


# ========== 日志系统（控制台彩色摘要 + 文件全量，启动清空） ==========
_LOG_LOCK = threading.RLock()
_LOG_FILE = None


def init_logging():
    """打开日志文件（w 模式 = 启动即清空旧日志）。不存在则自动创建。"""
    global _LOG_FILE
    with _LOG_LOCK:
        try:
            _LOG_FILE = open(LOG_FILE, "w", encoding="utf-8", buffering=1)
        except OSError as e:
            print("[日志] 无法创建日志文件 %s：%s" % (LOG_FILE, e), flush=True)
            _LOG_FILE = None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _file_write(level, tag, message):
    """写文件日志（线程安全，行缓冲自动 flush）。"""
    line = "[%s][%s][%s] %s: %s\n" % (_now(), level, threading.current_thread().name, tag, message)
    with _LOG_LOCK:
        f = _LOG_FILE
        if f is None:
            return
        try:
            f.write(line)
            f.flush()
        except OSError:
            pass


def log(color, tag, message):
    """常规日志：控制台彩色一行 + 文件 INFO 一行。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print("%s[%s]%s %s%s%s %s" % (C_DIM, ts, C_RESET, color, tag, C_RESET, message), flush=True)
    _file_write("INFO", tag, message)


def warn(color, tag, message):
    """警告日志：控制台 + 文件 WARN。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print("%s[%s]%s %s%s%s %s" % (C_DIM, ts, C_RESET, color, tag, C_RESET, message), flush=True)
    _file_write("WARN", tag, message)


def error(color, tag, message):
    """错误日志：控制台 + 文件 ERROR（处于异常上下文时附带调用栈，便于复盘）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print("%s[%s]%s %s%s%s %s" % (C_DIM, ts, C_RESET, color, tag, C_RESET, message), flush=True)
    _file_write("ERROR", tag, message)
    if sys.exc_info()[0] is not None:
        _file_write("ERROR", tag, "调用栈:\n" + traceback.format_exc())


def detail(tag, message):
    """全量明细日志：仅写文件（不刷控制台），用于记录完整回复内容等大数据。"""
    _file_write("DETAIL", tag, message)


# ========== 配置文件读写 ==========
CONFIG_TEMPLATE = """# 云端速递 · 云更新服务端配置：一行一个 键=值，# 开头为注释，改后重启生效

# TCP 服务端口（玩家客户端连接端口）
port=__PORT__

# 整合包内容版本号（客户端据此判断是否需要更新）
version=__VERSION__

# IPv4 监听地址：0.0.0.0 = 部署到所有 IPv4 地址（默认）；
# 可单独改成其它地址，例如 127.0.0.1 = 只在本机监听
bind_v4=0.0.0.0

# 是否启用 IPv6 监听：true = 启用（部署到所有 IPv6 地址）；false = 不部署 IPv6
enable_v6=true

# 文件下载最大并发数（1~64，防止大流量挤占其它服务）
max_downloads=10

# 客户端「问题反馈」按钮跳转链接（客户端每次更新完成后自动同步）
feedback_url=https://chat.deepseek.com/

# Web 管理端端口（浏览器访问 http://<服务器IP>:该端口；0 = 关闭管理端，只保留玩家更新服务）
# 管理端监听地址与上面的 bind_v4 / enable_v6 保持一致（改这两项需重启）
admin_port=10010

# Web 管理端登录密码（明文存储，便于随时修改；保存配置后立即生效）
admin_password=12345678
"""


def _migrate_legacy_version():
    """首次生成配置时：若存在旧版 version.txt 且首行是合法版本号，则带入配置。"""
    try:
        with open(LEGACY_VERSION_FILE, "r", encoding="utf-8-sig", errors="ignore") as f:
            first = f.readline().strip()
        if VERSION_RE.match(first):
            return first
    except OSError:
        pass
    return None


def ensure_config_file():
    """配置文件不存在则自动生成（含中文注释模板）。"""
    if os.path.exists(CONFIG_FILE):
        return
    version = _migrate_legacy_version() or DEFAULT_VERSION
    content = CONFIG_TEMPLATE.replace("__PORT__", str(DEFAULT_PORT)).replace("__VERSION__", version)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8-sig") as f:
            f.write(content)
        if version != DEFAULT_VERSION:
            detail("配置", "未找到配置文件，已自动生成 %s（版本号已从旧版 version.txt 迁移：%s）" % (CONFIG_FILE, version))
        else:
            detail("配置", "未找到配置文件，已自动生成默认配置：%s" % CONFIG_FILE)
    except OSError as e:
        error(C_RED, "配置", "自动生成配置文件失败：%s" % e)


def _parse_bool(raw, key, default):
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    warn(C_YELLOW, "配置", "配置项 %s 的值 [%s] 无法识别（应为 true/false），已回退默认 %s" % (key, raw, default))
    return default


def load_config():
    """读取配置文件（线程安全：启动时调用一次，运行期间不再读取）。"""
    ensure_config_file()
    cfg = {
        "port": DEFAULT_PORT,
        "version": DEFAULT_VERSION,
        "bind_v4": DEFAULT_BIND_V4,
        "enable_v6": DEFAULT_ENABLE_V6,
        "max_downloads": DEFAULT_MAX_DOWNLOADS,
        "feedback_url": DEFAULT_FEEDBACK_URL,
        "admin_port": DEFAULT_ADMIN_PORT,
        "admin_password": DEFAULT_ADMIN_PASSWORD,
    }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        error(C_RED, "配置", "读取配置文件失败（使用默认配置）：%s" % e)
        return cfg

    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warn(C_YELLOW, "配置", "第 %d 行格式错误（应为 键=值），已忽略：%s" % (lineno, line))
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not key or not value:
            warn(C_YELLOW, "配置", "第 %d 行键或值为空，已忽略：%s" % (lineno, line))
            continue
        if key == "port":
            try:
                p = int(value)
                if 1 <= p <= 65535:
                    cfg["port"] = p
                else:
                    warn(C_YELLOW, "配置", "port 超出范围(1~65535)：%s，使用默认 %d" % (value, DEFAULT_PORT))
            except ValueError:
                warn(C_YELLOW, "配置", "port 不是数字：%s，使用默认 %d" % (value, DEFAULT_PORT))
        elif key == "version":
            if VERSION_RE.match(value):
                cfg["version"] = value
            else:
                warn(C_YELLOW, "配置", "version 格式非法 [%s]（只允许「数字.数字.数字」三段格式，例如 1.0.001），使用默认 %s" % (value, DEFAULT_VERSION))
        elif key == "admin_port":
            try:
                ap = int(value)
                if 0 <= ap <= 65535:
                    cfg["admin_port"] = ap
                else:
                    warn(C_YELLOW, "配置", "admin_port 超出范围(0~65535)：%s，使用默认 %d" % (value, DEFAULT_ADMIN_PORT))
            except ValueError:
                warn(C_YELLOW, "配置", "admin_port 不是数字：%s，使用默认 %d" % (value, DEFAULT_ADMIN_PORT))
        elif key == "admin_password":
            if value:
                cfg["admin_password"] = value
            else:
                warn(C_YELLOW, "配置", "admin_password 为空，使用默认密码（请尽快在配置文件中修改）")
        elif key == "bind_v4":
            cfg["bind_v4"] = value
        elif key == "enable_v6":
            cfg["enable_v6"] = _parse_bool(value, "enable_v6", DEFAULT_ENABLE_V6)
        elif key == "max_downloads":
            try:
                n = int(value)
                if 1 <= n <= 64:
                    cfg["max_downloads"] = n
                else:
                    warn(C_YELLOW, "配置", "max_downloads 超出范围(1~64)：%s，使用默认 %d" % (value, DEFAULT_MAX_DOWNLOADS))
            except ValueError:
                warn(C_YELLOW, "配置", "max_downloads 不是数字：%s，使用默认 %d" % (value, DEFAULT_MAX_DOWNLOADS))
        elif key == "feedback_url":
            if value.startswith(("http://", "https://")):
                cfg["feedback_url"] = value
            else:
                warn(C_YELLOW, "配置", "feedback_url 不是合法 http(s) 链接 [%s]，使用默认 %s" % (value, DEFAULT_FEEDBACK_URL))
        else:
            warn(C_YELLOW, "配置", "第 %d 行：未知配置项 %s（已忽略，注意检查拼写）" % (lineno, key))
    return cfg


# ========== 重命名：文件统一加 _v<版本号>_ZAKO 尾缀（原 rename_to_version.bat 的 Python 复刻） ==========
# 规则（与原脚本一致）：
#   - mods 目录只处理 *.jar，resourcepacks 目录只处理 *.zip（不区分大小写扩展名）
#   - 只处理目录顶层文件，文件夹与其它文件一律不动
#   - 已带 _v<版本> 或 _v<版本>_ZAKO 尾缀的文件：自动替换成当前配置的版本号
#   - 可重复运行，不会叠加尾缀；目标同名已存在则跳过
_OLD_SUFFIX_RE = re.compile(r"_v[0-9][A-Za-z0-9._-]*(?:_ZAKO)?$", re.IGNORECASE)


def startup_rename(version):
    """服务端每次启动先执行一次重命名逻辑。返回 (处理数, 改名数, 跳过数, 失败数)。"""
    if not VERSION_RE.match(version):
        error(C_RED, "重命名", "版本号 %r 非法，本次启动跳过自动重命名（请检查配置文件 version 后重启）" % version)
        return 0, 0, 0, 0
    total = renamed = skipped = failed = 0
    suffix = "_v%s_ZAKO" % version
    for dirname, ext in RENAME_RULES:
        d = os.path.join(SCRIPT_DIR, dirname)
        if not os.path.isdir(d):
            detail("重命名", "目录不存在，跳过：%s" % dirname)
            continue
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            error(C_RED, "重命名", "无法读取目录 %s：%s" % (d, e))
            continue
        file_entries = [e for e in entries if e.is_file()]
        if not file_entries:
            detail("重命名", "目录中没有 *%s 文件：%s" % (ext, dirname))
            continue
        for entry in file_entries:
            name = entry.name
            if not name.lower().endswith(ext.lower()):
                continue
            total += 1
            base = name[:-len(ext)] if ext and name.lower().endswith(ext.lower()) else name
            # 去掉旧尾缀 _v<版本>（含 _ZAKO 变体），实现幂等替换
            base = _OLD_SUFFIX_RE.sub("", base)
            new_name = base + suffix + ext
            if new_name == name:
                skipped += 1
                continue
            target = os.path.join(d, new_name)
            if os.path.exists(target):
                skipped += 1
                detail("重命名", "跳过：目标已存在 %s -> %s" % (name, new_name))
                continue
            try:
                os.rename(entry.path, target)
                renamed += 1
                detail("重命名", "%s\\%s -> %s\\%s" % (dirname, name, dirname, new_name))
            except OSError as e:
                failed += 1
                warn(C_RED, "重命名", "改名失败：%s - %s" % (name, e))
    log(C_CYAN, "重命名", "自动重命名完成：共处理 %d 个文件，改名 %d，跳过 %d，失败 %d（尾缀 %s）"
        % (total, renamed, skipped, failed, suffix))
    if failed > 0:
        warn(C_YELLOW, "重命名", "有 %d 个文件改名失败，请查看日志（多为文件被占用）" % failed)
    return total, renamed, skipped, failed


# ========== 哈希清单缓存（后台增量重建，请求零阻塞） ==========
def _sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_snapshot():
    """目录快照（不计算哈希）：(相对路径, 大小, mtime_ns) 列表，用于检测文件增删改。"""
    snap = []
    for root, ext in MANIFEST_RULES:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(ext):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, SCRIPT_DIR).replace("\\", "/")
                try:
                    st = os.stat(full)
                    snap.append((rel, st.st_size, st.st_mtime_ns))
                except OSError:
                    pass
    snap.sort()
    return tuple(snap)


class ManifestIndex:
    """哈希 <-> 路径 双向索引。后台线程定期比对快照，变化时全量重建后原子替换引用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._map = {}     # 相对路径 -> sha1
        self._rev = {}     # sha1 -> 相对路径
        self._snap = None  # 最近一次快照
        self._built = False

    def _rebuild(self, reason, announce=False):
        """全量重建索引（在锁外做哈希计算前先取快照；完成后原子替换）。
        announce=True 时控制台打印构建结果（启动构建用）；后台自动重建仅写文件日志。"""
        detail("清单", "开始重建哈希清单（原因：%s）" % reason)
        t0 = time.time()
        snap = _dir_snapshot()
        entries = {}
        for root, ext in MANIFEST_RULES:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    if not fn.lower().endswith(ext):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, SCRIPT_DIR).replace("\\", "/")
                    try:
                        sha = _sha1_file(full)
                        entries[rel] = sha
                        detail("清单", "哈希计算 %s = %s" % (rel, sha))
                    except OSError as e:
                        warn(C_YELLOW, "清单", "哈希计算失败 %s：%s" % (rel, e))
        rev = {sha: rel for rel, sha in entries.items()}
        with self._lock:
            self._map = entries
            self._rev = rev
            self._snap = snap
            self._built = True
        msg = "哈希清单重建完成：共 %d 个文件，耗时 %.2f 秒（%s）" % (len(entries), time.time() - t0, reason)
        if announce:
            log(C_GREEN, "清单", msg)
        else:
            detail("清单", msg)

    def force_rebuild(self, reason="启动初始化"):
        """启动时强制全量构建（在重命名之后调用）；构建结果打印到控制台。"""
        self._rebuild(reason, announce=True)

    def refresh_if_changed(self):
        """比对快照，发现变化则重建（后台监测线程周期调用）；自动重建只写文件日志。"""
        snap = _dir_snapshot()
        with self._lock:
            old = self._snap
        if snap != old:
            self._rebuild("检测到分发目录文件变化", announce=False)

    def manifest(self, kind):
        """返回某类型（mods / resourcepacks）的 {相对路径: sha1} 字典（调用方直接序列化）。"""
        prefix = kind + "/"
        with self._lock:
            m = self._map
            return {rel: sha for rel, sha in m.items() if rel.startswith(prefix)}

    def path_by_hash(self, sha1):
        """按 sha1 反查相对路径（不存在返回空字符串）。"""
        with self._lock:
            return self._rev.get(sha1.lower(), "")

    def count(self):
        with self._lock:
            return len(self._map)


INDEX = ManifestIndex()


def read_changelog():
    """读取更新日志（UTF-8 容错 BOM）；文件缺失返回空字符串。"""
    try:
        with open(CHANGELOG_FILE, "r", encoding="utf-8-sig", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


# ========== 文件路径安全解析（防目录穿越） ==========
def resolve_update_file(rel):
    """把更新文件相对路径解析为真实路径；只允许白名单目录内的指定扩展名文件。"""
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if rel.startswith("mods/"):
        base, rest, ext = MODS_DIR, rel[len("mods/"):], ".jar"
    elif rel.startswith("shaderpacks/"):
        base, rest, ext = SHADERPACKS_DIR, rel[len("shaderpacks/"):], ".zip"
    elif rel.startswith("resourcepacks/"):
        base, rest, ext = RESOURCEPACKS_DIR, rel[len("resourcepacks/"):], ".zip"
    else:
        return None
    if not rest:
        return None
    full = os.path.realpath(os.path.join(base, rest))
    base_real = os.path.realpath(base)
    if full != base_real and not full.startswith(base_real + os.sep):
        return None
    if os.path.splitext(full)[1].lower() != ext:
        return None
    return full


# ========== TCP 指令处理 ==========
def recv_line(sock):
    """逐字节读取一行（限制长度），返回解码后的字符串；连接关闭返回空串。"""
    buf = bytearray()
    while len(buf) <= MAX_COMMAND_LEN:
        b = sock.recv(1)
        if not b:
            return "" if not buf else buf.decode("utf-8", errors="ignore").strip()
        if b == b"\n":
            break
        if b != b"\r":
            buf.append(b[0])
    if len(buf) > MAX_COMMAND_LEN:
        return None  # 超长行：视为恶意
    return buf.decode("utf-8", errors="ignore").strip()


def send_line(sock, text):
    sock.sendall((text + "\n").encode("utf-8"))


def handle_get_file(sock, peer, rel, download_sem):
    """GET_FILE：一行 FILE|<字节数> + 原始字节流，短连接传完即断。"""
    if not download_sem.acquire(blocking=False):
        send_line(sock, "ERROR|3|Server busy, please retry later")
        warn(C_YELLOW, "下载", "[%s] 下载并发已满，拒绝：%s" % (peer, rel))
        return
    try:
        path = resolve_update_file(rel)
        if path is None or not os.path.isfile(path):
            send_line(sock, "ERROR|1|File not found")
            warn(C_YELLOW, "下载", "[%s] 文件下载 404：%s" % (peer, rel))
            return
        size = os.path.getsize(path)
        # 传输阶段放宽超时：大文件经公网慢速传输可能超过指令读阶段
        sock.settimeout(FILE_SEND_TIMEOUT)
        send_line(sock, "FILE|%d" % size)
        t0 = time.time()
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(FILE_SEND_CHUNK)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
        elapsed = time.time() - t0
        speed = (sent / 1048576.0) / max(elapsed, 0.001) if sent else 0.0
        log(C_GREEN, "下载", "[%s] 文件下载完成 %s（%.2f MB，%.1f 秒，%.2f MB/s）"
            % (peer, rel, size / 1048576.0, elapsed, speed))
        detail("下载", "[%s] 文件 %s 共发送 %d 字节，与声明一致=%s" % (peer, rel, sent, sent == size))
    except (OSError, ConnectionError, socket.timeout) as e:
        # 客户端中途断开属正常场景（取消下载），黄色日志即可
        warn(C_YELLOW, "下载", "[%s] 文件发送中断 %s：%s" % (peer, rel, e))
    finally:
        download_sem.release()


def handle_connection(conn, addr, cfg, download_sem, pending_sem):
    """处理单条连接：读首行 -> GET_ 指令路由。全程异常兜底并记录。"""
    peer = "%s:%s" % (addr[0], addr[1])
    try:
        _record_client(addr[0])   # 今日连接统计（管理端概览用；一个 IP 一天只计一次）
        try:
            conn.settimeout(CONNECT_READ_TIMEOUT)
            first = recv_line(conn)
        except socket.timeout:
            warn(C_YELLOW, "连接", "[%s] 读取指令超时（%d 秒），已断开" % (peer, int(CONNECT_READ_TIMEOUT)))
            return
        if first is None:
            warn(C_YELLOW, "连接", "[%s] 指令行超长（>%d 字节），视为异常连接，已断开" % (peer, MAX_COMMAND_LEN))
            return
        if not first:
            return
        log(C_CYAN, "连接", "[%s] 收到指令：%s" % (peer, first[:120] + ("…" if len(first) > 120 else "")))
        if not first.startswith("GET_"):
            # 本服务端只提供云更新指令；非 GET_ 开头（如旧版验证协议）一律拒绝
            send_line(conn, "ERROR_UNKNOWN_COMMAND")
            warn(C_YELLOW, "连接", "[%s] 非云更新指令（本服务端不支持玩家验证），已拒绝" % peer)
            return

        if first == "GET_VERSION":
            version = cfg["version"]
            changelog = read_changelog()
            payload = json.dumps({"version": version, "changelog": changelog}, ensure_ascii=False)
            send_line(conn, payload)
            log(C_CYAN, "指令", "[%s] 查询版本号 -> %s" % (peer, version))
            detail("指令", "[%s] GET_VERSION 回复全文：%s" % (peer, payload))
        elif first == "GET_FEEDBACK_URL":
            url = cfg.get("feedback_url", DEFAULT_FEEDBACK_URL)
            payload = json.dumps({"feedbackUrl": url}, ensure_ascii=False)
            send_line(conn, payload)
            log(C_CYAN, "指令", "[%s] 查询反馈链接 -> %s" % (peer, url))
            detail("指令", "[%s] GET_FEEDBACK_URL 回复全文：%s" % (peer, payload))
        elif first.startswith("GET_MANIFEST|"):
            kind = first[len("GET_MANIFEST|"):].strip()
            if kind not in MANIFEST_KINDS:
                send_line(conn, "ERROR_UNKNOWN_COMMAND")
                warn(C_YELLOW, "指令", "[%s] 未知清单类型：%s" % (peer, kind))
            else:
                manifest = INDEX.manifest(kind)
                payload = json.dumps(manifest, ensure_ascii=False)
                send_line(conn, payload)
                log(C_CYAN, "指令", "[%s] 获取 %s 清单（%d 个文件）" % (peer, kind, len(manifest)))
                detail("指令", "[%s] GET_MANIFEST|%s 回复全文（含全部文件哈希）：%s" % (peer, kind, payload))
        elif first.startswith("GET_FILE|"):
            rel = first[len("GET_FILE|"):].strip()
            detail("指令", "[%s] GET_FILE 请求文件：%s" % (peer, rel))
            handle_get_file(conn, peer, rel, download_sem)
        elif first.startswith("GET_FILENAME|"):
            h = first[len("GET_FILENAME|"):].strip().lower()
            path = INDEX.path_by_hash(h)
            send_line(conn, path)
            log(C_CYAN, "指令", "[%s] 查询文件名 %s… -> %s" % (peer, h[:12], path or "(未找到)"))
            detail("指令", "[%s] GET_FILENAME|%s 回复：%s" % (peer, h, path))
        else:
            send_line(conn, "ERROR_UNKNOWN_COMMAND")
            warn(C_YELLOW, "指令", "[%s] 未知更新指令：%s" % (peer, first))
    except Exception:
        error(C_RED, "连接", "[%s] 处理连接时发生异常" % peer)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        pending_sem.release()


# ============================ Web 管理端（HTTP 10010） ============================
# 管理端与 10086 玩家服务同进程、共享 cfg 与哈希索引；HTTP 使用独立线程池与独立
# 信号量，因此管理操作不会占用玩家下载额度，管理端异常也不会影响玩家更新。
_RUNTIME = {"cfg": {}, "download_sem": None}
_START_TS = time.time()
_LAST_REBUILD = {"at": "", "sec": 0.0, "count": 0}


class ApiError(Exception):
    """管理端接口错误（带 HTTP 状态码）。"""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# ---------- 审计日志（独立文件，追加写，不随启动清空） ----------
_ADMIN_LOG_LOCK = threading.RLock()
_ADMIN_LOG_FILE = None


def init_admin_log():
    global _ADMIN_LOG_FILE
    with _ADMIN_LOG_LOCK:
        if _ADMIN_LOG_FILE is not None:
            return
        try:
            _ADMIN_LOG_FILE = open(ADMIN_AUDIT_FILE, "a", encoding="utf-8", buffering=1)
        except OSError as e:
            print("[管理端] 无法创建审计日志 %s：%s" % (ADMIN_AUDIT_FILE, e), flush=True)
            _ADMIN_LOG_FILE = None


def close_admin_log():
    global _ADMIN_LOG_FILE
    with _ADMIN_LOG_LOCK:
        f = _ADMIN_LOG_FILE
        _ADMIN_LOG_FILE = None
        if f is not None:
            try:
                f.flush()
                f.close()
            except OSError:
                pass


def admin_log(action, message, ip=""):
    """写审计日志并同步一条明细到运行日志（不含任何密码/令牌明文）。"""
    line = "%s | %s | %s | %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip or "-", action, message)
    with _ADMIN_LOG_LOCK:
        f = _ADMIN_LOG_FILE
        if f is not None:
            try:
                f.write(line + "\n")
                f.flush()
            except OSError:
                pass
    detail("管理端", "%s | %s" % (action, message))


# ---------- 登录会话（仅内存：服务端重启即全部失效） ----------
_SESSION_LOCK = threading.Lock()
_SESSIONS = {}          # token -> 过期时间戳
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILS = {}       # ip -> [连续失败次数, 锁定截止时间戳]


def _session_create():
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _SESSIONS[token] = time.time() + ADMIN_SESSION_TTL
    return token


def _session_valid(token):
    if not token:
        return False
    now = time.time()
    with _SESSION_LOCK:
        exp = _SESSIONS.get(token)
        if exp is None:
            return False
        if exp <= now:
            _SESSIONS.pop(token, None)
            return False
        return True


def _session_destroy(token):
    if not token:
        return
    with _SESSION_LOCK:
        _SESSIONS.pop(token, None)


def _sessions_clear():
    with _SESSION_LOCK:
        n = len(_SESSIONS)
        _SESSIONS.clear()
    return n


def _session_count():
    with _SESSION_LOCK:
        return len(_SESSIONS)


def _session_cleaner_loop():
    """后台兜底清理过期会话（读取时也会惰性删除）。"""
    while not _STOP.is_set():
        _STOP.wait(60.0)
        if _STOP.is_set():
            break
        try:
            now = time.time()
            with _SESSION_LOCK:
                for t in [k for k, exp in _SESSIONS.items() if exp <= now]:
                    _SESSIONS.pop(t, None)
        except Exception:
            error(C_RED, "管理端", "会话清理线程异常")


# ---------- 登录失败限速（同 IP 连续失败 5 次锁 5 分钟） ----------
def _login_locked(ip):
    """返回剩余锁定秒数（0 = 未被锁定）。"""
    now = time.time()
    with _LOGIN_LOCK:
        rec = _LOGIN_FAILS.get(ip)
        if not rec:
            return 0
        if rec[1] > now:
            return int(rec[1] - now) + 1
        return 0


def _login_failed(ip):
    """记录一次失败，返回该 IP 的连续失败次数。"""
    now = time.time()
    with _LOGIN_LOCK:
        if len(_LOGIN_FAILS) > 512:      # 只清理"锁定已过期"的条目，避免削弱限速
            for k in [k for k, v in _LOGIN_FAILS.items() if v[1] and v[1] <= now]:
                _LOGIN_FAILS.pop(k, None)
            while len(_LOGIN_FAILS) > 4096:   # 极端情况丢弃最早记录，保护内存
                _LOGIN_FAILS.pop(next(iter(_LOGIN_FAILS)), None)
        rec = _LOGIN_FAILS.get(ip)
        if rec is None or (rec[1] and rec[1] <= now):
            rec = [0, 0.0]
        rec[0] += 1
        if rec[0] >= ADMIN_LOGIN_MAX_FAIL:
            rec[1] = now + ADMIN_LOGIN_LOCK_SEC
        _LOGIN_FAILS[ip] = rec
        return rec[0]


def _login_ok(ip):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)


# ---------- 今日玩家连接统计（IP 去重，跨天自动重置） ----------
_STATS_LOCK = threading.Lock()
_STATS = {"day": "", "ips": set(), "conns": 0}


def _record_client(ip):
    """玩家客户端连接打点：累计连接次数 + IP 去重（同一天内）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _STATS_LOCK:
        if _STATS["day"] != today:
            _STATS["day"] = today
            _STATS["ips"] = set()
            _STATS["conns"] = 0
        _STATS["conns"] += 1
        if ip and len(_STATS["ips"]) < 100000:
            _STATS["ips"].add(ip)


def _stats_snapshot():
    today = datetime.now().strftime("%Y-%m-%d")
    with _STATS_LOCK:
        if _STATS["day"] != today:
            return {"ips": 0, "conns": 0}
        return {"ips": len(_STATS["ips"]), "conns": _STATS["conns"]}


_LAST_REBUILD_LOCK = threading.Lock()


def _mark_rebuild(t0=None, count=None):
    """记录最近一次清单重建的时间与耗时（管理端展示用）。"""
    with _LAST_REBUILD_LOCK:
        _LAST_REBUILD["at"] = datetime.now().strftime("%H:%M:%S")
        if t0 is not None:
            _LAST_REBUILD["sec"] = max(0.0, time.time() - t0)
        if count is not None:
            _LAST_REBUILD["count"] = count


# ---------- 后台任务：重建清单 / 完整重载（同一时刻只允许一个） ----------
_TASK_LOCK = threading.RLock()
_TASK = {"running": False, "kind": "", "steps": [], "error": None, "result": None, "started": 0.0}


def _task_steps(kind):
    if kind == "reload":
        return [{"name": n, "state": "pending"} for n in ADMIN_RELOAD_STEPS]
    return [{"name": "重建哈希清单", "state": "pending"}]


def _task_set_step(idx, state):
    with _TASK_LOCK:
        steps = _TASK["steps"]
        if 0 <= idx < len(steps):
            steps[idx]["state"] = state


def _task_running():
    with _TASK_LOCK:
        return bool(_TASK["running"])


def _task_snapshot():
    with _TASK_LOCK:
        return {
            "running": bool(_TASK["running"]),
            "kind": _TASK["kind"],
            "steps": [dict(s) for s in _TASK["steps"]],
            "error": _TASK["error"],
            "result": dict(_TASK["result"]) if _TASK["result"] else None,
        }


def start_task(kind="rebuild"):
    """启动后台任务；若已有任务在跑则返回 False（避免重命名/重建并发）。"""
    with _TASK_LOCK:
        if _TASK["running"]:
            return False
        _TASK.update({"running": True, "kind": kind, "steps": _task_steps(kind),
                      "error": None, "result": None, "started": time.time()})
    try:
        threading.Thread(target=_run_task, args=(kind,), name="管理端任务", daemon=True).start()
    except Exception:
        with _TASK_LOCK:
            _TASK["running"] = False
        error(C_RED, "管理端", "无法启动后台任务线程（%s）" % kind)
        return False
    return True


def _ensure_dist_dirs():
    """确保三个分发目录与 Changelog.txt 存在（与启动流程一致）。"""
    for d in (MODS_DIR, SHADERPACKS_DIR, RESOURCEPACKS_DIR):
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
                detail("管理端", "未找到 %s，已自动创建" % os.path.basename(d))
            except OSError as e:
                error(C_RED, "管理端", "创建目录失败 %s：%s" % (d, e))
    if not os.path.exists(CHANGELOG_FILE):
        try:
            with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
        except OSError as e:
            error(C_RED, "管理端", "创建 Changelog.txt 失败：%s" % e)


def _run_task(kind):
    """后台执行：rebuild = 只重建清单；reload = 重跑启动流程 + 重建清单。"""
    rename_stat = (0, 0, 0, 0)
    try:
        cfg = _RUNTIME.get("cfg") or {}
        if kind == "reload":
            _task_set_step(0, "doing")
            try:
                new_cfg = load_config()
                for k in ("version", "feedback_url", "admin_password"):
                    if k in new_cfg:
                        cfg[k] = new_cfg[k]
                detail("管理端", "重载：已重读配置文件（热载 version/feedback_url/admin_password）")
            except Exception:
                error(C_RED, "管理端", "重载时读取配置失败（沿用当前配置）")
            _task_set_step(0, "done")

            _task_set_step(1, "doing")
            _ensure_dist_dirs()
            _task_set_step(1, "done")

            _task_set_step(2, "doing")
            try:
                rename_stat = startup_rename(cfg.get("version", DEFAULT_VERSION)) or (0, 0, 0, 0)
            except Exception:
                error(C_RED, "管理端", "重载时自动重命名失败")
            _task_set_step(2, "done")

            _task_set_step(3, "doing")
            t0 = time.time()
            INDEX.force_rebuild("管理端重新加载")
            sec = time.time() - t0
            _mark_rebuild(t0, INDEX.count())
            _task_set_step(3, "done")
            _task_set_step(4, "done")
        else:
            _task_set_step(0, "doing")
            t0 = time.time()
            INDEX.force_rebuild("管理端操作后刷新清单")
            sec = time.time() - t0
            _mark_rebuild(t0, INDEX.count())
            _task_set_step(0, "done")

        result = {"manifestCount": INDEX.count(), "elapsedSec": sec, "at": _LAST_REBUILD["at"],
                  "renamed": rename_stat[1], "skipped": rename_stat[2], "failed": rename_stat[3]}
        with _TASK_LOCK:
            _TASK["result"] = result
        detail("管理端", "后台任务完成（%s）：清单 %d 个文件，耗时 %.2f 秒"
               % (kind, result["manifestCount"], result["elapsedSec"]))
    except Exception as e:
        error(C_RED, "管理端", "后台任务失败（%s）：%s" % (kind, e))
        with _TASK_LOCK:
            _TASK["error"] = "任务执行失败：%s" % e
            for _s in _TASK["steps"]:
                if _s["state"] == "doing":
                    _s["state"] = "fail"
    finally:
        with _TASK_LOCK:
            _TASK["running"] = False


# ---------- 分发目录文件操作（只允许顶层文件 + 扩展名白名单 + 目录穿越防护） ----------
def safe_kind_dir(kind):
    """返回 (目录绝对路径, 允许的扩展名)；未知类型抛 ApiError。"""
    if kind == "mods":
        return MODS_DIR, ".jar"
    if kind == "shaderpacks":
        return SHADERPACKS_DIR, ".zip"
    if kind == "resourcepacks":
        return RESOURCEPACKS_DIR, ".zip"
    raise ApiError(400, "未知目录类型：%s" % kind)


def safe_file_path(kind, name):
    """校验文件名并返回 (绝对路径, 相对路径)。只允许分发目录顶层的白名单文件。"""
    root, ext = safe_kind_dir(kind)
    name = str(name or "").strip()
    if not name:
        raise ApiError(400, "文件名不能为空")
    if len(name) > 200:
        raise ApiError(400, "文件名过长（最多 200 字符）")
    if ADMIN_NAME_RE.search(name) or "/" in name or "\\" in name:
        raise ApiError(400, '文件名不能包含 \\ / : * ? " < > | 等字符')
    if name.startswith(".") or name in (".", ".."):
        raise ApiError(400, "文件名不合法")
    if name.endswith(".") or name.endswith(" "):
        raise ApiError(400, "文件名不能以点或空格结尾")
    if os.path.splitext(name)[0].strip().upper() in ADMIN_WIN_RESERVED:
        raise ApiError(400, "文件名不合法（Windows 保留名）")
    if os.path.splitext(name)[1].lower() != ext:
        raise ApiError(400, "%s 目录只接受 %s 文件" % (kind, ext))
    base_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(full) != base_real:
        raise ApiError(400, "路径不合法")
    return full, kind + "/" + name


def list_kind_files(kind):
    """列出某分发目录的顶层文件（名称 / 大小 / SHA1）；子目录内容不参与管理。"""
    root, _ext = safe_kind_dir(kind)
    manifest = INDEX.manifest(kind)
    prefix = kind + "/"
    out = []
    for rel, sha in manifest.items():
        if not rel.startswith(prefix):
            continue
        rest = rel[len(prefix):]
        if not rest or "/" in rest:
            continue
        try:
            size = os.path.getsize(os.path.join(root, rest))
        except OSError:
            continue
        out.append({"name": rest, "size": size, "sha": sha})
    out.sort(key=lambda x: x["name"].lower())
    return out


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _cleanup_upload_tmp():
    """启动时清理上次异常退出残留的上传临时文件（*.part）。"""
    try:
        if not os.path.isdir(ADMIN_UPLOAD_TMP):
            return
        n = 0
        for fn in os.listdir(ADMIN_UPLOAD_TMP):
            if fn.endswith(".part"):
                _safe_remove(os.path.join(ADMIN_UPLOAD_TMP, fn))
                n += 1
        if n:
            detail("管理端", "已清理 %d 个残留上传临时文件" % n)
    except OSError:
        pass


def _replace_file(src, dst, attempts=3, delay=0.4):
    """把临时文件原子替换到目标位置；Windows 上目标被下载占用时重试。"""
    last_err = ""
    for _i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return True, ""
        except PermissionError:
            last_err = "文件正在被下载或占用，请稍后重试"
            time.sleep(delay)
        except OSError as e:
            last_err = "替换文件失败：%s" % e
            break
    return False, last_err or "替换文件失败"


def _is_ipv4(s):
    parts = str(s or "").split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not re.match(r"^[0-9]{1,3}$", p):
            return False
        if int(p) > 255:
            return False
    return True


# ---------- 配置文件写回（保留注释与顺序，原子替换） ----------
_CONFIG_WRITE_LOCK = threading.RLock()


def write_config_values(updates):
    """按行改写 speedupdate.conf（串行化 + 原子替换，避免并发保存互相覆盖）。"""
    with _CONFIG_WRITE_LOCK:
        return _write_config_values_inner(updates)


def _write_config_values_inner(updates):
    """按行改写 speedupdate.conf：已存在的键就地替换，缺失的键追加到末尾。

    返回 {键: 旧值}（用于判断哪些项发生了变化）。写入使用临时文件 + 原子替换，
    避免写一半导致配置文件损坏。
    """
    for _k, _v in updates.items():
        if "\r" in str(_v) or "\n" in str(_v):
            raise ApiError(400, "配置项 %s 的值不能包含换行符" % _k)
    ensure_config_file()
    try:
        with open(CONFIG_FILE, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ApiError(500, "读取配置文件失败：%s" % e)
    has_bom = raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    old = {}
    seen = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key = s.split("=", 1)[0].strip().lower()
        if key in updates:
            old[key] = s.split("=", 1)[1].strip()
            lines[i] = "%s=%s" % (key, updates[key])
            seen.add(key)
    for k, v in updates.items():
        if k not in seen:
            lines.append("%s=%s" % (k, v))
    data = newline.join(lines) + newline
    tmp = CONFIG_FILE + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "wb") as f:
            if has_bom:
                f.write(codecs.BOM_UTF8)
            f.write(data.encode("utf-8"))
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        _safe_remove(tmp)
        raise ApiError(500, "写入配置文件失败：%s" % e)
    detail("管理端", "配置文件已更新：%s" % "、".join(sorted(updates.keys())))
    return old


def write_changelog(text):
    """写入 Changelog.txt（UTF-8 无 BOM，LF 换行；临时文件 + 原子替换）。"""
    tmp = CHANGELOG_FILE + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CHANGELOG_FILE)
    except OSError as e:
        _safe_remove(tmp)
        raise ApiError(500, "写入 Changelog.txt 失败：%s" % e)


# ---------- 日志增量读取 ----------
def tail_log_file(path, offset):
    """读取日志增量。offset<=0 或越界时返回文件尾部若干行（reset=True）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"lines": [], "offset": 0, "size": 0, "reset": True}
    try:
        if offset <= 0 or offset > size:
            start = max(0, size - 512 * 1024)      # 只读尾部 512KB，避免大日志整文件读入
            with open(path, "rb") as f:
                if start:
                    f.seek(start)
                data = f.read()
            if start:
                cut = data.find(b"\n")             # 丢弃可能被截断的首行
                data = data[cut + 1:] if cut >= 0 else b""
            lines = data.decode("utf-8", "replace").splitlines()
            return {"lines": lines[-ADMIN_LOG_PAGE_LINES:], "offset": size, "size": size, "reset": True}
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return {"lines": lines[-ADMIN_LOG_PAGE_LINES:], "offset": size, "size": size, "reset": False}
    except OSError:
        return {"lines": [], "offset": 0, "size": 0, "reset": True}


# ---------- 概览状态 ----------
def _active_downloads(maxdl):
    """当前下载占用数（由下载信号量剩余许可反推；读取失败按 0 处理）。"""
    sem = _RUNTIME.get("download_sem")
    try:
        return max(0, int(maxdl) - int(sem._value))
    except Exception:
        return 0


def build_state():
    """管理端概览数据。"""
    cfg = _RUNTIME.get("cfg") or {}
    files = {}
    total_bytes = 0
    for kind in ("mods", "shaderpacks", "resourcepacks"):
        try:
            lst = list_kind_files(kind)
        except ApiError:
            lst = []
        files[kind] = len(lst)
        total_bytes += sum(x["size"] for x in lst)
    files["total"] = files["mods"] + files["shaderpacks"] + files["resourcepacks"]
    files["bytes"] = total_bytes
    stats = _stats_snapshot()
    with _LAST_REBUILD_LOCK:
        last_at = _LAST_REBUILD["at"]
        last_sec = _LAST_REBUILD["sec"]
    maxdl = int(cfg.get("max_downloads", DEFAULT_MAX_DOWNLOADS) or DEFAULT_MAX_DOWNLOADS)
    return {
        "version": cfg.get("version", DEFAULT_VERSION),
        "serverVersion": SERVER_VERSION,
        "port": cfg.get("port", DEFAULT_PORT),
        "bindV4": cfg.get("bind_v4", DEFAULT_BIND_V4),
        "enableV6": bool(cfg.get("enable_v6", DEFAULT_ENABLE_V6)),
        "maxDownloads": maxdl,
        "activeDownloads": _active_downloads(maxdl),
        "adminPort": cfg.get("admin_port", DEFAULT_ADMIN_PORT),
        "files": files,
        "manifestCount": INDEX.count(),
        "lastRebuild": last_at,
        "lastRebuildSec": last_sec,
        "todayIps": stats["ips"],
        "todayConns": stats["conns"],
        "uptimeSec": int(time.time() - _START_TS),
        "changelogLen": len(read_changelog()),
        "confPath": os.path.basename(CONFIG_FILE),
        "rebuilding": _task_running(),
    }


# ---------- HTTP 处理 ----------
def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class AdminHandler(BaseHTTPRequestHandler):
    """管理端 HTTP 处理器。

    鉴权策略：只有「登录页」与「登录接口」是公开的，其余任何请求
    （包括静态资源、状态查询、文件读写）都必须携带有效会话 Cookie。
    """

    protocol_version = "HTTP/1.1"
    server_version = "CloudExpressAdmin/" + SERVER_VERSION
    sys_version = ""
    timeout = ADMIN_BODY_TIMEOUT

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        detail("管理端", "HTTP %s %s" % (self.address_string(), fmt % args))

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ""

    def _cfg(self):
        return _RUNTIME.get("cfg") or {}

    def _security_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.send_header("Cache-Control", "no-store")

    def _send_bytes(self, code, body, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for k, v in (extra or ()):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, socket.timeout, OSError):
            pass

    def _send_json(self, code, obj, extra=None):
        self._send_bytes(code, _json_bytes(obj), "application/json; charset=utf-8", extra)

    def _send_error_json(self, code, msg):
        self._send_json(code, {"error": msg})

    def _send_html(self):
        self._send_bytes(200, ADMIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

    def _redirect(self, location):
        self._send_bytes(302, b"", "text/plain; charset=utf-8", [("Location", location)])

    def _cookie_token(self):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _sep, v = part.strip().partition("=")
            if k == ADMIN_COOKIE_NAME:
                return v.strip()
        return ""

    def _same_origin_ok(self):
        """CSRF 纵深防御：跨站发起的写请求一律拒绝（SameSite 之外再加一层）。"""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True          # 非浏览器客户端（脚本/curl）不带 Origin，由会话鉴权把关
        host = (self.headers.get("Host") or "").strip().lower()
        return origin.lower() in ("http://" + host, "https://" + host)

    def _read_json_body(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "请求长度不合法")
        if length <= 0:
            return {}
        if length > limit:
            self.close_connection = True
            raise ApiError(413, "请求体过大")
        data = self.rfile.read(length)
        if not data:
            return {}
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "请求体格式不正确")
        return obj

    # ---- 请求入口 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._send_bytes(405, _json_bytes({"error": "不支持的请求方法"}),
                         "application/json; charset=utf-8", [("Allow", "GET, POST, HEAD")])

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def _dispatch(self, method):
        """连接数已在服务器 accept 阶段用信号量限流（见 _AdminHTTPServer.process_request）。"""
        self._dispatch_inner(method)

    def _dispatch_inner(self, method):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path or "/")
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            ip = self._client_ip()

            # ---- 公开路由：登录页与登录接口（其余一律需要有效会话） ----
            if path == "/login" and method == "GET":
                self._send_html()
                return
            if path == "/api/login" and method == "POST":
                self._handle_login(ip)
                return

            token = self._cookie_token()
            if not _session_valid(token):
                if path.startswith("/api/"):
                    self._send_error_json(401, "未登录或登录状态已过期")
                else:
                    self._redirect("/login")
                return

            if method == "POST" and not self._same_origin_ok():
                self._send_error_json(403, "请求来源校验失败")
                return

            # ---- 已登录：业务路由 ----
            if method == "GET" and path in ("/", "/index.html"):
                self._send_html()
            elif method == "GET" and path == "/api/state":
                self._send_json(200, build_state())
            elif method == "GET" and path == "/api/files":
                self._handle_list_files(query)
            elif method == "GET" and path == "/api/file/download":
                self._handle_admin_download(query)
            elif method == "GET" and path == "/api/version":
                self._send_json(200, {"version": self._cfg().get("version", DEFAULT_VERSION),
                                      "changelog": read_changelog()})
            elif method == "GET" and path == "/api/config":
                self._handle_get_config()
            elif method == "GET" and path == "/api/logs":
                self._handle_logs(query)
            elif method == "GET" and path == "/api/reload/status":
                self._send_json(200, _task_snapshot())
            elif method == "POST" and path == "/api/logout":
                _session_destroy(token)
                admin_log("退出登录", "会话已销毁", ip)
                self._send_json(200, {"ok": True}, [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                                                     % ADMIN_COOKIE_NAME)])
            elif method == "POST" and path == "/api/upload":
                self._handle_upload(query, ip)
            elif method == "POST" and path == "/api/file/rename":
                self._handle_rename(ip)
            elif method == "POST" and path == "/api/file/delete":
                self._handle_delete(ip)
            elif method == "POST" and path == "/api/version":
                self._handle_save_version(ip)
            elif method == "POST" and path == "/api/config":
                self._handle_save_config(ip)
            elif method == "POST" and path == "/api/reload":
                if start_task("reload"):
                    admin_log("重新加载", "已启动后台重载任务（重跑启动流程）", ip)
                    self._send_json(200, {"ok": True, "started": True})
                else:
                    self._send_error_json(409, "已有任务正在执行，请稍候再试")
            else:
                self._send_error_json(404, "接口不存在")
        except ApiError as e:
            # 若请求体尚未读取（上传/登录等提前拒绝），必须关闭连接，
            # 否则残留字节会被 keep-alive 当成下一个请求解析，造成响应错位。
            try:
                _has_body = int(self.headers.get("Content-Length") or 0) > 0
            except ValueError:
                _has_body = True
            if _has_body:
                self.close_connection = True
            self._send_error_json(e.code, e.message)
        except (BrokenPipeError, ConnectionError, socket.timeout):
            pass
        except Exception as e:
            error(C_RED, "管理端", "处理管理端请求异常：%s" % e)
            try:
                self._send_error_json(500, "服务端内部错误")
            except Exception:
                pass

    # ---- 登录 / 退出 ----
    def _handle_login(self, ip):
        remain = _login_locked(ip)
        if remain > 0:
            admin_log("登录被拒", "IP 处于锁定期（剩余 %d 秒）" % remain, ip)
            self._send_error_json(429, "登录失败次数过多，请 %d 秒后再试" % remain)
            return
        data = self._read_json_body()
        pwd = str(data.get("password") or "")
        expect = str(self._cfg().get("admin_password", DEFAULT_ADMIN_PASSWORD) or "")
        if not pwd or not hmac.compare_digest(pwd.encode("utf-8"), expect.encode("utf-8")):
            fails = _login_failed(ip)
            left = max(0, ADMIN_LOGIN_MAX_FAIL - fails)
            admin_log("登录失败", "密码错误（连续第 %d 次，剩余 %d 次机会）" % (fails, left), ip)
            if fails >= ADMIN_LOGIN_MAX_FAIL:
                self._send_error_json(429, "密码错误次数过多，已锁定 %d 分钟" % (ADMIN_LOGIN_LOCK_SEC // 60))
            else:
                self._send_error_json(401, "密码错误")
            return
        _login_ok(ip)
        token = _session_create()
        admin_log("登录成功", "会话有效期 %d 小时" % (ADMIN_SESSION_TTL // 3600), ip)
        self._send_json(200, {"ok": True, "ttl": ADMIN_SESSION_TTL},
                        [("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                          % (ADMIN_COOKIE_NAME, token, ADMIN_SESSION_TTL))])

    # ---- 文件列表 / 下载 ----
    def _handle_list_files(self, query):
        kind = (query.get("kind") or [""])[0]
        files = list_kind_files(kind)
        self._send_json(200, {"kind": kind, "total": len(files), "files": files})

    def _handle_admin_download(self, query):
        kind = (query.get("kind") or [""])[0]
        name = (query.get("name") or [""])[0]
        full, rel = safe_file_path(kind, name)
        if not os.path.isfile(full):
            raise ApiError(404, "文件不存在")
        if self.command == "HEAD":
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self._security_headers()
            self.end_headers()
            return
        try:
            f = open(full, "rb")
        except OSError as e:
            raise ApiError(404, "文件无法读取：%s" % e)
        try:
            # 先打开再取长度，避免 getsize 与 open 之间文件被替换导致长度不符
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self._security_headers()
            self.end_headers()
            sent = 0
            while True:
                chunk = f.read(FILE_SEND_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
            admin_log("下载文件", "%s（%d 字节）" % (rel, sent), self._client_ip())
        except (BrokenPipeError, ConnectionError, socket.timeout):
            self.close_connection = True
            warn(C_YELLOW, "管理端", "管理端下载中断：%s" % rel)
        except OSError as e:
            self.close_connection = True
            error(C_RED, "管理端", "管理端下载失败 %s：%s" % (rel, e))
        finally:
            try:
                f.close()
            except OSError:
                pass

    # ---- 上传（原始字节流，非 multipart） ----
    def _handle_upload(self, query, ip):
        kind = (query.get("kind") or [""])[0]
        name = (query.get("name") or [""])[0]
        overwrite = (query.get("overwrite") or ["0"])[0].lower() in ("1", "true", "yes", "on")
        full, rel = safe_file_path(kind, name)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ApiError(411, "缺少 Content-Length（请通过管理端页面上传）")
        if length > ADMIN_MAX_UPLOAD:
            self._discard_body(length)
            raise ApiError(413, "文件过大（上限 %d MB）" % (ADMIN_MAX_UPLOAD // 1048576))
        if os.path.isdir(full):
            self._discard_body(length)
            raise ApiError(409, "目标位置存在同名目录，无法上传")
        try:
            free = shutil.disk_usage(SCRIPT_DIR).free
            if free < length + 64 * 1024 * 1024:
                self._discard_body(length)
                raise ApiError(507, "服务器磁盘剩余空间不足（剩余 %d MB）" % (free // 1048576))
        except OSError:
            pass
        exists = os.path.isfile(full)
        if exists and not overwrite:
            self._discard_body(length)
            raise ApiError(409, "云端已存在同名文件")
        try:
            os.makedirs(ADMIN_UPLOAD_TMP, exist_ok=True)
        except OSError as e:
            raise ApiError(500, "无法创建上传临时目录：%s" % e)
        tmp = os.path.join(ADMIN_UPLOAD_TMP, uuid.uuid4().hex + ".part")
        sha = hashlib.sha1()
        got = 0
        try:
            with open(tmp, "wb") as f:
                remain = length
                while remain > 0:
                    chunk = self.rfile.read(min(FILE_SEND_CHUNK, remain))
                    if not chunk:
                        raise IOError("客户端连接中断")
                    f.write(chunk)
                    sha.update(chunk)
                    got += len(chunk)
                    remain -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
        except (OSError, IOError, socket.timeout) as e:
            _safe_remove(tmp)
            self.close_connection = True
            raise ApiError(500, "上传写入失败：%s" % e)
        ok, err = _replace_file(tmp, full)
        if not ok:
            _safe_remove(tmp)
            raise ApiError(423, err)
        admin_log("覆盖文件" if exists else "上传文件",
                  "%s（%d 字节，sha1=%s）" % (rel, got, sha.hexdigest()), ip)
        start_task("rebuild")
        self._send_json(200, {"ok": True, "size": got, "sha": sha.hexdigest(), "overwritten": exists})

    def _discard_body(self, length, limit=64 * 1024 * 1024):
        """尽量读掉请求体，避免 keep-alive 下残留字节污染后续请求。

        body 过大（超过 limit）时放弃读取并关闭连接——客户端仍在发送，
        继续读取只会白白消耗带宽与磁盘。
        """
        if length <= 0:
            return
        if length > limit:
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(FILE_SEND_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, socket.timeout):
            self.close_connection = True

    # ---- 改名 / 删除 ----
    def _handle_rename(self, ip):
        data = self._read_json_body()
        kind = str(data.get("kind") or "")
        name = str(data.get("name") or "")
        new_name = str(data.get("newName") or "").strip()
        old_full, old_rel = safe_file_path(kind, name)
        new_full, new_rel = safe_file_path(kind, new_name)
        if new_rel == old_rel:
            raise ApiError(400, "新名称与原名相同")
        if not os.path.isfile(old_full):
            raise ApiError(404, "文件不存在")
        if os.path.exists(new_full):
            raise ApiError(409, "目标文件名已存在")
        try:
            os.rename(old_full, new_full)
        except PermissionError:
            raise ApiError(423, "文件正在被下载或占用，请稍后重试")
        except OSError as e:
            raise ApiError(500, "改名失败：%s" % e)
        warning = ""
        if not re.search(r"_ZAKO\.[A-Za-z0-9]+$", new_name, re.IGNORECASE):
            warning = "新文件名不含 _ZAKO 尾缀，客户端将不再管理此文件（既不下发也不删除）。"
        admin_log("改名文件", "%s -> %s" % (old_rel, new_rel), ip)
        start_task("rebuild")
        self._send_json(200, {"ok": True, "warning": warning})

    def _handle_delete(self, ip):
        data = self._read_json_body()
        kind = str(data.get("kind") or "")
        name = str(data.get("name") or "")
        full, rel = safe_file_path(kind, name)
        if not os.path.isfile(full):
            raise ApiError(404, "文件不存在")
        try:
            os.remove(full)
        except PermissionError:
            raise ApiError(423, "文件正在被下载或占用，请稍后重试")
        except OSError as e:
            raise ApiError(500, "删除失败：%s" % e)
        admin_log("删除文件", rel, ip)
        start_task("rebuild")
        self._send_json(200, {"ok": True})

    # ---- 版本号 / Changelog ----
    def _handle_save_version(self, ip):
        data = self._read_json_body()
        version = str(data.get("version") or "").strip()
        if not VERSION_RE.match(version):
            raise ApiError(400, "版本号必须为 数字.数字.数字 三段格式，例如 1.0.001")
        changelog = data.get("changelog")
        changelog = read_changelog() if changelog is None else str(changelog).replace("\r\n", "\n")
        if len(changelog) > 20000:
            raise ApiError(400, "更新日志过长（上限 20000 字符）")
        do_rename = data.get("rename") is True
        if do_rename and _task_running():
            raise ApiError(409, "已有后台任务正在执行，请稍候再试")
        old = write_config_values({"version": version})
        write_changelog(changelog)
        self._cfg()["version"] = version
        admin_log("修改内容版本号", "%s -> %s%s" % (old.get("version", "?"), version,
                                                "（同步文件尾缀）" if do_rename else ""), ip)
        admin_log("保存更新日志", "%d 字符" % len(changelog), ip)
        if do_rename:
            started = start_task("reload")
            payload = {"ok": True, "background": started, "version": version}
            if not started:
                payload["warning"] = "后台任务繁忙，文件尾缀未同步；请稍后点击「重新加载」"
            self._send_json(200, payload)
            return
        self._send_json(200, {"ok": True, "renamed": 0, "manifestCount": INDEX.count(),
                              "elapsedSec": 0.0, "version": version})

    # ---- 配置读写 ----
    def _handle_get_config(self):
        cfg = self._cfg()
        self._send_json(200, {"config": {
            "port": cfg.get("port", DEFAULT_PORT),
            "bind_v4": cfg.get("bind_v4", DEFAULT_BIND_V4),
            "enable_v6": bool(cfg.get("enable_v6", DEFAULT_ENABLE_V6)),
            "max_downloads": cfg.get("max_downloads", DEFAULT_MAX_DOWNLOADS),
            "feedback_url": cfg.get("feedback_url", DEFAULT_FEEDBACK_URL),
            "admin_port": cfg.get("admin_port", DEFAULT_ADMIN_PORT),
        }})

    def _handle_save_config(self, ip):
        data = self._read_json_body()
        cfg = self._cfg()
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            raise ApiError(400, "服务端口必须是 1~65535 的整数")
        if not (1 <= port <= 65535):
            raise ApiError(400, "服务端口必须是 1~65535 的整数")
        bind_v4 = str(data.get("bind_v4") or "").strip()
        if not _is_ipv4(bind_v4):
            raise ApiError(400, "监听地址必须是合法 IPv4，例如 0.0.0.0 或 127.0.0.1")
        raw_v6 = data.get("enable_v6")
        if isinstance(raw_v6, bool):
            enable_v6 = raw_v6
        else:
            enable_v6 = str(raw_v6 or "").strip().lower() in ("true", "1", "yes", "on")
        try:
            maxdl = int(data.get("max_downloads"))
        except (TypeError, ValueError):
            raise ApiError(400, "下载并发上限必须是 1~64 的整数")
        if not (1 <= maxdl <= 64):
            raise ApiError(400, "下载并发上限必须是 1~64 的整数")
        feedback = str(data.get("feedback_url") or "").strip()
        if not feedback.startswith(("http://", "https://")):
            raise ApiError(400, "反馈链接必须以 http:// 或 https:// 开头")
        if "\r" in feedback or "\n" in feedback or len(feedback) > 500:
            raise ApiError(400, "反馈链接不合法（不能含换行，长度不超过 500）")
        try:
            admin_port = int(data.get("admin_port"))
        except (TypeError, ValueError):
            raise ApiError(400, "管理端口必须是 0~65535 的整数")
        if not (0 <= admin_port <= 65535):
            raise ApiError(400, "管理端口必须是 0~65535 的整数")

        new_pwd = data.get("admin_password")
        password_changed = False
        if new_pwd is not None and str(new_pwd) != "":
            new_pwd = str(new_pwd)
            if len(new_pwd) > 128:
                raise ApiError(400, "管理密码过长（上限 128 字符）")
            if "\r" in new_pwd or "\n" in new_pwd:
                raise ApiError(400, "管理密码不能包含换行符")
            cur = str(cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD) or "")
            if not hmac.compare_digest(new_pwd.encode("utf-8"), cur.encode("utf-8")):
                password_changed = True

        updates = {
            "port": port,
            "bind_v4": bind_v4,
            "enable_v6": "true" if enable_v6 else "false",
            "max_downloads": maxdl,
            "feedback_url": feedback,
            "admin_port": admin_port,
        }
        if password_changed:
            updates["admin_password"] = new_pwd
        old = write_config_values(updates)

        restart_keys = []
        for key, new_val in (("port", str(port)), ("bind_v4", bind_v4),
                             ("enable_v6", "true" if enable_v6 else "false"),
                             ("max_downloads", str(maxdl)), ("admin_port", str(admin_port))):
            if str(old.get(key, "")).strip().lower() != new_val.lower():
                restart_keys.append(key)

        # 可热载项立即生效；端口/监听/并发上限需重启（信号量与监听套接字在启动时创建）
        cfg["feedback_url"] = feedback
        if password_changed:
            cfg["admin_password"] = new_pwd
            cleared = _sessions_clear()
            admin_log("修改管理密码", "所有登录状态已失效（清除 %d 个会话）" % cleared, ip)
        admin_log("保存配置", "需重启项：%s" % ("、".join(restart_keys) if restart_keys else "无"), ip)
        self._send_json(200, {"ok": True, "restartKeys": restart_keys, "passwordChanged": password_changed})

    # ---- 日志 ----
    def _handle_logs(self, query):
        which = (query.get("which") or ["run"])[0]
        if which not in ("run", "admin"):
            raise ApiError(400, "未知日志类型：%s" % which)
        path = LOG_FILE if which == "run" else ADMIN_AUDIT_FILE
        try:
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            offset = 0
        data = tail_log_file(path, offset)
        data["ok"] = True
        data["which"] = which
        self._send_json(200, data)


# ---------- 管理端服务器 ----------
_ADMIN_SERVERS = []
ADMIN_MAX_PENDING = 32                                   # 管理端同时处理的请求上限（防洪水）
_ADMIN_CONN_SEM = threading.BoundedSemaphore(ADMIN_MAX_PENDING)


class _AdminHTTPServer(ThreadingHTTPServer):
    """管理端 HTTP 服务器：连接数在 accept 阶段就受信号量约束（含只连不发的空连接）。"""

    daemon_threads = True
    allow_reuse_address = True

    def process_request(self, request, client_address):
        if not _ADMIN_CONN_SEM.acquire(blocking=False):
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            _ADMIN_CONN_SEM.release()
            raise

    def shutdown_request(self, request):
        try:
            ThreadingHTTPServer.shutdown_request(self, request)
        finally:
            _ADMIN_CONN_SEM.release()

    def handle_error(self, request, client_address):
        detail("管理端", "HTTP 连接异常：%s" % (client_address,))


class _AdminHTTPServerV6(_AdminHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        ThreadingHTTPServer.server_bind(self)


def start_admin_server(cfg):
    """启动管理端 HTTP 服务（IPv4 + 可选 IPv6，与玩家服务一致的监听地址）。"""
    port = int(cfg.get("admin_port", DEFAULT_ADMIN_PORT) or 0)
    if port <= 0:
        log(C_YELLOW, "管理端", "配置 admin_port=0：管理端已关闭（仅提供玩家更新服务）")
        return []
    if (not ADMIN_HTML) or ("__ADMIN_HTML_PLACEHOLDER__" in ADMIN_HTML):
        error(C_RED, "管理端", "管理端页面资源缺失，管理端未启动")
        return []
    bind_v4 = cfg.get("bind_v4", DEFAULT_BIND_V4) or "0.0.0.0"
    init_admin_log()
    _cleanup_upload_tmp()
    targets = [(_AdminHTTPServer, bind_v4, "IPv4", C_RED)]
    if cfg.get("enable_v6"):
        targets.append((_AdminHTTPServerV6, "::", "IPv6", C_YELLOW))
    banners = []
    for cls, host, label, color in targets:
        try:
            srv = cls((host, port), AdminHandler)
        except OSError as e:
            error(color, "管理端", "%s 管理端启动失败（%s:%d）：%s" % (label, host, port, e))
            continue
        _ADMIN_SERVERS.append(srv)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.4},
                         name="管理端HTTP(%s)" % label, daemon=True).start()
        banners.append("http://%s:%d（%s）" % ("[::]" if host == "::" else host, port, label))
    if banners:
        log(C_GREEN, "管理端", "管理端已就绪：%s" % " / ".join(banners))
        log(C_GREEN, "管理端", "登录会话有效期 %d 小时；连续失败 %d 次锁定 %d 分钟"
            % (ADMIN_SESSION_TTL // 3600, ADMIN_LOGIN_MAX_FAIL, ADMIN_LOGIN_LOCK_SEC // 60))
    return banners


def stop_admin_servers():
    """停止管理端 HTTP 服务并关闭审计日志。"""
    for srv in list(_ADMIN_SERVERS):
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    _ADMIN_SERVERS.clear()
    close_admin_log()


# ========== 服务器骨架 ==========
_STOP = threading.Event()


def accept_loop(listen_sock, family_label, cfg, download_sem, pending_sem, executor):
    """监听循环：accept 后提交到线程池（超出排队上限直接拒绝并断开）。"""
    detail("监听", "%s 已就绪：等待连接…" % family_label)
    while not _STOP.is_set():
        try:
            conn, addr = listen_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            if _STOP.is_set():
                break
            warn(C_YELLOW, "监听", "%s accept 出错，0.2 秒后重试" % family_label)
            time.sleep(0.2)
            continue
        if _STOP.is_set():
            try:
                conn.close()
            except OSError:
                pass
            break
        if not pending_sem.acquire(blocking=False):
            # 服务繁忙：直接断开，避免无界排队拖垮进程
            warn(C_YELLOW, "连接", "[%s] 连接排队已满（%d），拒绝并断开"
                 % ("%s:%s" % (addr[0], addr[1]), MAX_PENDING_CONNS))
            try:
                conn.close()
            except OSError:
                pass
            continue
        executor.submit(handle_connection, conn, addr, cfg, download_sem, pending_sem)


def bind_listeners(cfg):
    """按配置绑定监听套接字。返回 (监听套接字列表, 展示文本列表)。"""
    socks = []
    banners = []
    # ---- IPv4 ----
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s4.bind((cfg["bind_v4"], cfg["port"]))
        s4.listen(BACKLOG)
        s4.settimeout(0.5)  # 让 accept 周期醒来检查停止标志
        socks.append(s4)
        v4_text = "IPv4 %s:%d" % (cfg["bind_v4"], cfg["port"])
        if cfg["bind_v4"] in ("0.0.0.0", ""):
            v4_text += "（全部 IPv4 地址）"
        banners.append(v4_text)
        detail("监听", "IPv4 监听成功：%s" % v4_text)
    except OSError as e:
        error(C_RED, "监听", "IPv4 绑定失败（%s:%d）：%s" % (cfg["bind_v4"], cfg["port"], e))
        try:
            s4.close()
        except OSError:
            pass
    # ---- IPv6（可选，独立 socket + V6ONLY，与 IPv4 互不干扰） ----
    if cfg["enable_v6"]:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        try:
            s6.bind(("::", cfg["port"]))
            s6.listen(BACKLOG)
            s6.settimeout(0.5)
            socks.append(s6)
            banners.append("IPv6 [::]:%d（全部 IPv6 地址）" % cfg["port"])
            detail("监听", "IPv6 监听成功：[::]:%d（全部 IPv6 地址）" % cfg["port"])
        except OSError as e:
            warn(C_YELLOW, "监听", "IPv6 监听失败（本机可能不支持 IPv6），仅提供 IPv4 服务：%s" % e)
            try:
                s6.close()
            except OSError:
                pass
    else:
        detail("监听", "配置 enable_v6=false：不监听 IPv6")
    return socks, banners


def watcher_loop():
    """后台清单监测：周期比对目录快照，变化则自动重建哈希索引。"""
    while not _STOP.is_set():
        _STOP.wait(WATCH_INTERVAL)
        if _STOP.is_set():
            break
        try:
            INDEX.refresh_if_changed()
        except Exception:
            error(C_RED, "清单", "后台清单监测异常")


def banner(cfg):
    line = "=" * 46
    print(C_CYAN + C_BOLD + line + C_RESET)
    print(C_BOLD + "  云端速递 · 整合包云更新服务端 v" + SERVER_VERSION + C_RESET)
    print(C_GREEN + "  服务端口 : TCP %d" % cfg["port"] + C_RESET)
    v4_desc = "全部 IPv4 地址" if cfg["bind_v4"] in ("0.0.0.0", "") else ("仅 " + cfg["bind_v4"])
    if cfg["enable_v6"]:
        print(C_GREEN + "  监听地址 : IPv4 %s（%s）| IPv6 已启用（[::] 全部 IPv6）" % (cfg["bind_v4"], v4_desc) + C_RESET)
    else:
        print(C_GREEN + "  监听地址 : IPv4 %s（%s，IPv6 已关闭）" % (cfg["bind_v4"], v4_desc) + C_RESET)
    print(C_GREEN + "  内容版本 : %s" % cfg["version"] + C_RESET)
    dist_text = " / ".join("%s\\*.%s" % (name, ext.lstrip(".")) for name, ext in RENAME_RULES)
    print(C_YELLOW + "  分发目录 : " + dist_text + C_RESET)
    print(C_YELLOW + "  更新日志 : %s（不存在时返回空）" % os.path.basename(CHANGELOG_FILE) + C_RESET)
    print(C_YELLOW + "  配置文件 : %s（修改后重启生效）" % os.path.basename(CONFIG_FILE) + C_RESET)
    _admin_port = cfg.get("admin_port", DEFAULT_ADMIN_PORT)
    if _admin_port:
        print(C_MAGENTA + "  管理端   : HTTP %d（浏览器访问 http://<服务器IP>:%d）" % (_admin_port, _admin_port) + C_RESET)
    else:
        print(C_YELLOW + "  管理端   : 已关闭（admin_port=0）" + C_RESET)
    print(C_MAGENTA + "  运行日志 : %s（每次启动清空重建）" % os.path.basename(LOG_FILE) + C_RESET)
    print(C_CYAN + C_BOLD + line + C_RESET)
    print("")
    sys.stdout.flush()  # 横幅必须实时显示（重定向/缓冲场景下也确保写出）


def main():
    _enable_ansi_windows()
    # 1. 初始化日志文件（清空旧日志）
    init_logging()
    detail("系统", "云端速递云更新服务端启动（日志文件：%s）" % LOG_FILE)
    # 2. 加载 / 生成配置
    cfg = load_config()
    detail("配置", "生效配置：port=%d version=%s bind_v4=%s enable_v6=%s max_downloads=%d"
           % (cfg["port"], cfg["version"], cfg["bind_v4"], cfg["enable_v6"], cfg["max_downloads"]))
    # 3. 确保分发目录与 Changelog.txt 存在（不存在则自动创建空的，静默执行）
    for d in (MODS_DIR, SHADERPACKS_DIR, RESOURCEPACKS_DIR):
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
                detail("初始化", "未找到 %s 目录，已自动创建" % os.path.basename(d))
            except OSError as e:
                error(C_RED, "初始化", "创建目录失败 %s：%s" % (d, e))
    if not os.path.exists(CHANGELOG_FILE):
        try:
            with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
            detail("初始化", "未找到 Changelog.txt，已自动创建空文件（UTF-8）")
        except OSError as e:
            error(C_RED, "初始化", "创建 Changelog.txt 失败：%s" % e)
    # 4. 横幅（只显示云端速递名称与配置内容）
    banner(cfg)
    # 5. 启动先执行一次自动重命名（_v<版本>_ZAKO）
    dirs_text = "、".join("%s/*%s" % (name, ext) for name, ext in RENAME_RULES)
    detail("重命名", "开始启动自动重命名（给 %s 加 _v%s_ZAKO 尾缀）" % (dirs_text, cfg["version"]))
    startup_rename(cfg["version"])
    # 6. 构建哈希清单（在重命名之后，保证索引文件名带新尾缀）
    _rebuild_t0 = time.time()
    INDEX.force_rebuild("启动初始化")
    _mark_rebuild(_rebuild_t0, INDEX.count())
    # 7. 绑定监听
    download_sem = threading.BoundedSemaphore(cfg["max_downloads"])
    socks, banners = bind_listeners(cfg)
    if not socks:
        error(C_RED, "系统", "没有任何监听套接字可用，服务无法启动，请检查配置与端口占用后重试")
        log(C_YELLOW, "系统", "按回车键退出…")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return
    # 8. 启动连接线程池 / 监听线程 / 清单监测线程
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                  thread_name_prefix="云更新连接")
    pending_sem = threading.BoundedSemaphore(MAX_PENDING_CONNS)
    _STOP.clear()
    for s in socks:
        threading.Thread(target=accept_loop,
                         args=(s, "监听(%s)" % s.family, cfg, download_sem, pending_sem, executor),
                         name="监听线程", daemon=True).start()
    threading.Thread(target=watcher_loop, name="清单监测", daemon=True).start()
    # 8.5 启动 Web 管理端（默认 10010；监听地址复用 bind_v4 / enable_v6）
    _RUNTIME["cfg"] = cfg
    _RUNTIME["download_sem"] = download_sem
    threading.Thread(target=_session_cleaner_loop, name="会话清理", daemon=True).start()
    start_admin_server(cfg)
    detail("系统", "服务启动完成：%s" % " / ".join(banners))
    # 9. 主线程等待退出信号（Ctrl+C）
    try:
        while not _STOP.wait(0.5):
            pass
    except KeyboardInterrupt:
        log(C_YELLOW, "系统", "收到退出信号，正在关闭…")
    # 10. 收尾：停止监听与线程池（不等待挂起任务，直接退出，防卡死）
    _STOP.set()
    for s in socks:
        try:
            s.close()
        except OSError:
            pass
    executor.shutdown(wait=False, cancel_futures=True)
    stop_admin_servers()
    log(C_YELLOW, "系统", "服务已停止")
    with _LOG_LOCK:
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
                _LOG_FILE.close()
            except OSError:
                pass
    # os._exit：跳过解释器退出时对线程池的等待（部分连接仍在传输也不卡退出）
    os._exit(0)


if __name__ == "__main__":
    main()
