# -*- coding: utf-8 -*-
"""
云端速递 · 整合包云更新服务端（独立版）

背景：早期开发时把「云端速递」的云更新功能直接塞进了 mod2（生存作弊小套餐）
共用的 verify_server.py。本文件是从中独立出来的「云端速递」专属服务端：
只提供整合包云更新能力（TCP 指令协议），不含 mod2 旧项目的玩家验证与 Web 管理面板。

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
  * 可靠性设计：有界线程池处理连接（防线程爆炸）、下载并发信号量限流、
    哈希清单由后台线程增量重建（服务请求零阻塞）、socket 超时兜底、
    所有工作线程 daemon 化、配置读取线程安全。

使用方法：双击运行本文件（需要 Python 3）。修改配置后重启生效。
更新内容：mods/ 放 .jar、shaderpacks/ 与 resourcepacks/ 放 .zip，版本号改 speedupdate.conf 的 version，
更新日志写 Changelog.txt（可选，不存在则返回空日志）。
"""
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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

# 版本号合法性（与旧 rename 脚本一致：字母/数字/点/下划线/短横线，不能以符号开头）
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

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
                warn(C_YELLOW, "配置", "version 格式非法 [%s]（只允许字母、数字、点、下划线、短横线，不能以符号开头），使用默认 %s" % (value, DEFAULT_VERSION))
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
    print(C_BOLD + "  云端速递 · 整合包云更新服务端 v2.0.0" + C_RESET)
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
    INDEX.force_rebuild("启动初始化")
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
