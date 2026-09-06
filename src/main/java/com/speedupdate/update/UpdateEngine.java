package com.speedupdate.update;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.speedupdate.SpeedUpdate;
import net.minecraft.client.Minecraft;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.net.Socket;

/**
 * 云更新流程引擎。
 *
 * 设计要点：
 *  - 所有网络 / 磁盘操作都在独立工作线程（单线程串行，避免状态竞争）
 *  - UI 状态字段为 volatile，由工作线程写入、主线程渲染读取
 *  - 状态变化通过 Minecraft.getInstance().execute() 回到主线程通知界面
 *  - 取消：关闭正在使用的 Socket 使阻塞调用立即抛异常；等待重试用可中断的 await
 */
public class UpdateEngine {
    /** 窗口状态（与开发文档第七章一致，7 种状态）。 */
    public enum Phase {
        REQUESTING,       // 请求中
        ERROR,            // 网络异常
        UPDATE_AVAILABLE, // 有新版本
        UP_TO_DATE,       // 已是最新
        DOWNLOADING,      // 更新下载中
        DONE,             // 更新完成
        FAILED            // 更新失败
    }

    /** UI 状态变化回调（运行在主线程）。 */
    public interface Listener {
        void onStateChanged();
    }

    private static final int VERSION_TIMEOUT_MS = 10_000;
    private static final int MANIFEST_TIMEOUT_MS = 5_000;
    private static final int FILE_READ_TIMEOUT_MS = 60_000;  // 文件下载读超时：大文件慢网传输需要更宽松的超时
    private static final int MAX_ATTEMPTS = 4;   // 1 次下载 + 3 次重试
    private static final long RETRY_DELAY_MS = 10_000L;
    private static final long SILENT_DELETE_BUDGET_MS = 15_000L;  // 静默预删总时长上限，防止大列表拖慢「更新完成」

    private final ServerConfig config;
    private final ExecutorService executor =
            Executors.newSingleThreadExecutor(r -> {
                Thread t = new Thread(r, "SpeedUpdate-Worker");
                t.setDaemon(true);
                return t;
            });

    private final AtomicBoolean cancelled = new AtomicBoolean(false);
    private final AtomicReference<Socket> activeSocket = new AtomicReference<>();
    private final AtomicReference<Socket> activeDownloadSocket = new AtomicReference<>();
    private volatile CountDownLatch sleepLatch = new CountDownLatch(0);
    private volatile Listener listener;

    // ---- UI 状态（工作线程写，主线程读）----
    public volatile Phase phase = Phase.REQUESTING;
    public volatile String remoteVersion = "";
    public volatile String changelog = "";
    public volatile String localVersion = "";
    public volatile String statusLine = "";      // “正在下载 模组 文件（第 X / Y 个）”
    public volatile String currentFile = "";     // 当前文件相对路径
    public volatile float progress = 0f;         // 0.0 ~ 1.0（文件数加权 + 文件内字节比例，平滑推进）
    public volatile long currentDownloaded = 0;  // 当前文件已下载字节
    public volatile long currentTotal = 0;       // 当前文件总字节
    public volatile double currentSpeedBps = 0;  // 当前下载速度（字节/秒）
    private volatile long lastSpeedBytes = 0;    // 速度采样：上次采样字节
    private volatile long lastSpeedTime = 0;     // 速度采样：上次采样时间
    public volatile String failReason = "";
    public volatile boolean hasPendingDeletions = false;

    /** true=修复模式（严格对齐云端，删除玩家自装文件）；false=正常更新（只管理带 _ZAKO 标识的云端文件）。 */
    private volatile boolean repairMode = false;

    public UpdateEngine() {
        this.config = ServerConfig.load();
        SpeedUpdate.LOGGER.info("[云更新] 服务端配置：{}", config);
    }

    public ServerConfig getConfig() {
        return config;
    }

    /** 界面注册 / 注销回调（界面关闭时必须注销）。 */
    public void setListener(Listener listener) {
        this.listener = listener;
    }

    /** 取消所有进行中的操作：立即关闭连接，中止下载 / 重试等待。 */
    public void cancel() {
        cancelled.set(true);
        closeQuietly(activeSocket.getAndSet(null));
        closeQuietly(activeDownloadSocket.getAndSet(null));
        sleepLatch.countDown();
        SpeedUpdate.LOGGER.info("[云更新] 收到取消请求，已断开所有连接");
    }

    /**
     * 「取消下载」按钮专用：断开连接并清空缓存目录。
     * 先 cancel() 使阻塞中的读写立即抛异常退出，再把缓存清理任务排队到同一
     * 单线程 executor，保证严格按「先退出下载、再清空临时文件」顺序执行。
     * 已通过 SHA-1 校验并同步到正式目录的文件不受影响；版本号不会写入
     * （版本号本就只在全部成功后写入）。
     */
    public void cancelDownloadAndClean() {
        cancel();
        executor.submit(() -> {
            clearCacheDir(UpdatePaths.CACHE_MODS_DIR);
            clearCacheDir(UpdatePaths.CACHE_SHADERPACKS_DIR);
            clearCacheDir(UpdatePaths.CACHE_RESOURCEPACKS_DIR);
            SpeedUpdate.LOGGER.info("[云更新] 已取消下载，缓存目录已清空");
        });
    }

    /** 重置取消标记（窗口重新打开 / 重新开始更新时调用）。 */
    private void resetCancelled() {
        cancelled.set(false);
    }

    private void postStateChange() {
        Minecraft.getInstance().execute(() -> {
            Listener l = listener;
            if (l != null) {
                l.onStateChanged();
            }
        });
    }

    /** 打开窗口 → 自动请求版本号（10 秒超时）。 */
    public void checkVersion() {
        resetCancelled();
        setPhase(Phase.REQUESTING);
        executor.submit(() -> {
            try {
                localVersion = UpdatePaths.readVersion();
                String raw = UpdateNetwork.tcpCommand(config.host, config.tcpPort, "GET_VERSION",
                        VERSION_TIMEOUT_MS, activeSocket);
                JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
                String remote = obj.has("version") ? obj.get("version").getAsString() : "";
                String log = obj.has("changelog") ? obj.get("changelog").getAsString() : "";
                if (remote.isBlank()) {
                    throw new IOException("服务端返回的版本号为空");
                }
                remoteVersion = remote;
                changelog = log == null ? "" : log;
                SpeedUpdate.LOGGER.info("[云更新] 服务端版本 {}，本地版本 {}", remoteVersion, localVersion);
                boolean newer = VersionNumber.parse(remoteVersion).isNewerThan(localVersion);
                setPhase(newer ? Phase.UPDATE_AVAILABLE : Phase.UP_TO_DATE);
            } catch (Exception e) {
                SpeedUpdate.LOGGER.warn("[云更新] 查询版本号失败：{}", e.toString());
                if (!cancelled.get()) {
                    setPhase(Phase.ERROR);
                }
            }
        });
    }

    /** 点击「立刻更新 / 重试」→ 正常更新：只管理带 _ZAKO 标识的云端文件，玩家自装文件不受影响。 */
    public void startUpdate() {
        repairMode = false;
        doStartUpdate();
    }

    /** 「修复客户端」→ 严格将客户端与云端对齐（含删除玩家自装内容；本质仍是一次完整更新）。 */
    public void startRepair() {
        repairMode = true;
        doStartUpdate();
    }

    /** 「重试」：沿用上一次的模式（修复流程失败后点重试仍按修复模式走，避免中途悄悄切换）。 */
    public void startRetry() {
        doStartUpdate();
    }

    private void doStartUpdate() {
        resetCancelled();
        setPhase(Phase.DOWNLOADING);
        progress = 0f;
        currentDownloaded = 0;
        currentTotal = 0;
        currentSpeedBps = 0;
        executor.submit(() -> {
            // 防御：若版本号尚未获取（异常路径直接触发更新），先请求一次再执行流程
            if (remoteVersion.isBlank()) {
                try {
                    String raw = UpdateNetwork.tcpCommand(config.host, config.tcpPort, "GET_VERSION",
                            VERSION_TIMEOUT_MS, activeSocket);
                    JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
                    String remote = obj.has("version") ? obj.get("version").getAsString() : "";
                    if (remote.isBlank()) {
                        throw new IOException("服务端返回的版本号为空");
                    }
                    remoteVersion = remote;
                    if (obj.has("changelog")) {
                        changelog = obj.get("changelog").getAsString();
                    }
                } catch (Exception e) {
                    SpeedUpdate.LOGGER.warn("[云更新] 更新前获取版本号失败：{}", e.toString());
                    if (!cancelled.get()) {
                        setPhase(Phase.ERROR);
                    }
                    return;
                }
            }
            runUpdateFlow();
        });
    }

    private void setPhase(Phase p) {
        phase = p;
        postStateChange();
    }

    private void refreshProgress() {
        postStateChange();
    }

    /**
     * 每次更新完成（写版本号）前，向云端请求一次「问题反馈」链接并写入本地配置。
     * 复用 tcpCommand 短连接（读完即关、可取消）；失败静默忽略，不影响更新主流程。
     * 用短超时（5 秒）——它只是收尾同步，不能因此拖慢「更新完成」出现。
     */
    private void refreshFeedbackUrl() {
        try {
            String raw = UpdateNetwork.tcpCommand(config.host, config.tcpPort, "GET_FEEDBACK_URL",
                    MANIFEST_TIMEOUT_MS, activeSocket);
            JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
            String url = obj.has("feedbackUrl") ? obj.get("feedbackUrl").getAsString() : "";
            if (url != null && !url.isBlank()) {
                UpdatePaths.writeFeedbackUrl(url);
                SpeedUpdate.LOGGER.info("[云更新] 已同步云端反馈链接：{}", url);
            }
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 同步反馈链接失败（忽略，保持本地配置）：{}", e.toString());
        }
    }

    // ==================== 完整更新流程 ====================

    private void runUpdateFlow() {
        try {
            SpeedUpdate.LOGGER.info("[云更新] 开始完整更新流程");
            // 11.1 准备工作：清空缓存目录
            clearCacheDirectories();

            // 提前获取三个清单：进度条总文件数需要知道三个阶段的总和（顺序：模组 → 光影包 → 资源包）
            Map<String, String> modsManifest = fetchManifest("mods");
            if (modsManifest == null) {
                return; // 失败已处理
            }
            Map<String, String> spsManifest = fetchManifest("shaderpacks");
            if (spsManifest == null) {
                return;
            }
            Map<String, String> dpsManifest = fetchManifest("resourcepacks");
            if (dpsManifest == null) {
                return;
            }

            // 本地比对（正常更新只扫带 _ZAKO 标识的云端文件；修复模式扫描全部，严格对齐）
            Map<String, String> localMods = scanLocal(UpdatePaths.MODS_DIR, ".jar", !repairMode);
            Map<String, String> localSps = scanLocal(UpdatePaths.SHADERPACKS_DIR, ".zip", !repairMode);
            Map<String, String> localDps = scanLocal(UpdatePaths.RESOURCEPACKS_DIR, ".zip", !repairMode);

            List<Map.Entry<String, String>> modsToDownload = diffToDownload(modsManifest, localMods);
            List<String> modsToDelete = diffToDelete(modsManifest, localMods);
            List<Map.Entry<String, String>> spsToDownload = diffToDownload(spsManifest, localSps);
            List<String> spsToDelete = diffToDelete(spsManifest, localSps);
            List<Map.Entry<String, String>> dpsToDownload = diffToDownload(dpsManifest, localDps);
            List<String> dpsToDelete = diffToDelete(dpsManifest, localDps);

            int total = modsToDownload.size() + spsToDownload.size() + dpsToDownload.size();
            SpeedUpdate.LOGGER.info("[云更新] 差异分析：mods 下载 {} 删除 {}，shaderpacks 下载 {} 删除 {}，resourcepacks 下载 {} 删除 {}",
                    modsToDownload.size(), modsToDelete.size(),
                    spsToDownload.size(), spsToDelete.size(),
                    dpsToDownload.size(), dpsToDelete.size());

            List<String> pendingDeletions = new ArrayList<>();
            int[] done = {0};

            // ---- 阶段一：mods ----
            if (!modsToDownload.isEmpty()) {
                boolean ok = downloadPhase("模组", "mods", modsToDownload, total, done);
                if (!ok) {
                    fail("模组文件下载失败，请检查网络后重试");
                    return;
                }
            }
            // mods 下载成功（或无需下载）：立即写入 mods 待删除列表
            pendingDeletions.addAll(modsToDelete);
            UpdatePaths.writePending(pendingDeletions);

            // ---- 阶段二：shaderpacks（光影包，插入在资源包之前） ----
            if (!spsToDownload.isEmpty()) {
                boolean ok = downloadPhase("光影包", "shaderpacks", spsToDownload, total, done);
                if (!ok) {
                    fail("光影包文件下载失败，请检查网络后重试");
                    return;
                }
            }
            pendingDeletions.addAll(spsToDelete);
            UpdatePaths.writePending(pendingDeletions);

            // ---- 阶段三：resourcepacks ----
            if (!dpsToDownload.isEmpty()) {
                boolean ok = downloadPhase("资源包", "resourcepacks", dpsToDownload, total, done);
                if (!ok) {
                    fail("资源包文件下载失败，请检查网络后重试");
                    return;
                }
            }
            pendingDeletions.addAll(dpsToDelete);
            UpdatePaths.writePending(pendingDeletions);

            // ---- 全部成功 ----
            // 写版本号前静默预删：游戏已运行一段时间，旧文件多半已不被占用，
            // 此时试删成功率远高于启动时（成败不提示、列表内容不变，删不掉留给启动流程）
            silentPreDelete(pendingDeletions);
            refreshFeedbackUrl();   // 云端推送「问题反馈」链接（失败静默，不阻断更新）
            UpdatePaths.writeVersion(remoteVersion);
            hasPendingDeletions = !pendingDeletions.isEmpty();
            progress = 1f;
            UpdateStatus.newVersionAvailable = false;   // 更新完成：主界面按钮恢复默认样式
            SpeedUpdate.LOGGER.info("[云更新] 更新完成，版本号已写入 {}，待删除 {} 个",
                    remoteVersion, pendingDeletions.size());
            setPhase(Phase.DONE);
        } catch (Exception e) {
            SpeedUpdate.LOGGER.error("[云更新] 更新流程异常：", e);
            if (!cancelled.get()) {
                fail("更新过程发生未知错误，请重试");
            }
        }
    }

    /**
     * 下载某个阶段（mods / shaderpacks / resourcepacks）的全部文件。
     *
     * @return 是否全部成功
     */
    private boolean downloadPhase(String label, String kind, List<Map.Entry<String, String>> toDownload,
                                  int total, int[] done) {
        // 每类内容独立映射「缓存目录 / 正式目录」，杜绝类型错落
        Path cacheRoot = cacheDirFor(kind);
        Path finalRoot = targetDirFor(kind);
        int index = 0;
        int phaseTotal = toDownload.size();
        for (Map.Entry<String, String> entry : toDownload) {
            if (cancelled.get()) {
                return false;
            }
            index++;
            String rel = entry.getKey();          // 如 mods/Example.jar
            String expectedSha1 = entry.getValue();
            String rest = rel.substring(kind.length() + 1); // 去掉 "mods/" 前缀
            statusLine = "正在下载 " + label + " 文件（第 " + index + " / " + phaseTotal + " 个）";
            currentFile = rel;
            refreshProgress();

            Path cacheTarget = cacheRoot.resolve(rest);
            Path finalTarget = finalRoot.resolve(rest);

            // 实时进度：每个文件开始时重置字节采样
            currentDownloaded = 0;
            currentTotal = 0;
            currentSpeedBps = 0;
            lastSpeedBytes = 0;
            lastSpeedTime = System.currentTimeMillis();

            boolean success = false;
            for (int attempt = 1; attempt <= MAX_ATTEMPTS && !success; attempt++) {
                if (cancelled.get()) {
                    return false;
                }
                try {
                    Files.createDirectories(cacheTarget.getParent());
                    Files.deleteIfExists(cacheTarget);
                    SpeedUpdate.LOGGER.info("[云更新] 下载 {}（第 {} 次尝试）", rel, attempt);
                    UpdateNetwork.tcpFileDownload(config.host, config.tcpPort, rel, cacheTarget,
                            FILE_READ_TIMEOUT_MS, activeDownloadSocket, cancelled, (received, totalBytes) -> {
                                // 工作线程回调：只更新 volatile 字段，UI 每帧自行读取渲染（无需事件通知）
                                currentDownloaded = received;
                                currentTotal = totalBytes;
                                long now = System.currentTimeMillis();
                                long dt = now - lastSpeedTime;
                                if (dt >= 500) {
                                    currentSpeedBps = (received - lastSpeedBytes) * 1000.0 / Math.max(1, dt);
                                    lastSpeedBytes = received;
                                    lastSpeedTime = now;
                                }
                                // 文件数加权进度：已完成文件数 + 当前文件内字节比例
                                progress = total > 0
                                        ? (float) ((done[0] + (double) received / Math.max(1, totalBytes)) / total)
                                        : 0f;
                            });

                    // SHA-1 校验
                    String actual = sha1(cacheTarget);
                    if (!expectedSha1.equalsIgnoreCase(actual)) {
                        throw new IOException("哈希校验失败：期望 " + expectedSha1 + "，实际 " + actual);
                    }

                    // 移动到正式目录；目标被游戏占用（如资源包已被加载）时移动失败，进入统一重试逻辑
                    Files.createDirectories(finalTarget.getParent());
                    moveReplace(cacheTarget, finalTarget);
                    success = true;
                } catch (Exception e) {
                    SpeedUpdate.LOGGER.warn("[云更新] {} 第 {} 次尝试失败：{}", rel, attempt, e.toString());
                    if (!cancelled.get() && attempt < MAX_ATTEMPTS) {
                        sleepInterruptibly(RETRY_DELAY_MS);
                    }
                }
            }

            if (!success) {
                // 回滚：清空本阶段缓存（已移动走的不受影响）
                clearCacheDir(cacheRoot);
                return false;
            }

            done[0]++;
            progress = total > 0 ? (float) done[0] / total : 0f;
            refreshProgress();
        }
        return true;
    }

    /**
     * 静默预删：更新流程全部成功后、写入版本号之前，对待删除列表做一次
     * 「每文件最多 3 次、间隔 1 秒」的静默删除尝试（玩家无感知，不改 UI、不改列表）。
     *
     * 为什么有效：启动时旧 jar 可能被游戏加载句柄或杀毒软件瞬时占用，而此刻
     * 游戏已运行良久，多数占用早已释放，成功机会远高于启动时。
     * 删不掉的条目原样保留（不重写 pending_deletions.json），由「启动重试 + 退出兜底」处理。
     * 总耗时上限 15 秒，超出或玩家取消时立即返回，不拖慢「更新完成」出现。
     */
    private void silentPreDelete(List<String> rels) {
        if (rels.isEmpty()) {
            return;
        }
        long deadline = System.currentTimeMillis() + SILENT_DELETE_BUDGET_MS;
        for (String rel : rels) {
            if (cancelled.get() || System.currentTimeMillis() > deadline) {
                return;
            }
            Path target = UpdatePaths.GAME_DIR.resolve(rel).normalize();
            // 与启动删除完全相同的安全边界：只允许删除 mods/、shaderpacks/ 或 resourcepacks/ 下的文件
            if (!target.startsWith(UpdatePaths.GAME_DIR)
                    || !(target.startsWith(UpdatePaths.MODS_DIR) || target.startsWith(UpdatePaths.SHADERPACKS_DIR)
                            || target.startsWith(UpdatePaths.RESOURCEPACKS_DIR))) {
                continue;
            }
            for (int attempt = 1; attempt <= 3; attempt++) {
                if (cancelled.get() || System.currentTimeMillis() > deadline) {
                    return;
                }
                try {
                    if (Files.deleteIfExists(target) && !Files.exists(target)) {
                        SpeedUpdate.LOGGER.info("[云更新] 更新前静默预删成功：{}", rel);
                        break;
                    }
                } catch (IOException e) {
                    SpeedUpdate.LOGGER.debug("[云更新] 静默预删未成功（第 {} 次）：{} - {}", attempt, rel, e.getMessage());
                }
                if (attempt < 3) {
                    sleepInterruptibly(1000L);
                }
            }
        }
    }

    private void fail(String reason) {
        failReason = reason;
        SpeedUpdate.LOGGER.error("[云更新] 更新失败：{}", reason);
        setPhase(Phase.FAILED);
    }

    // ==================== 工具方法 ====================

    /** 内容类型 → 下载缓存目录（mods/shaderpacks/resourcepacks）。 */
    private static Path cacheDirFor(String kind) {
        return switch (kind) {
            case "mods" -> UpdatePaths.CACHE_MODS_DIR;
            case "shaderpacks" -> UpdatePaths.CACHE_SHADERPACKS_DIR;
            case "resourcepacks" -> UpdatePaths.CACHE_RESOURCEPACKS_DIR;
            default -> throw new IllegalArgumentException("未知内容类型：" + kind);
        };
    }

    /** 内容类型 → 正式安装目录。 */
    private static Path targetDirFor(String kind) {
        return switch (kind) {
            case "mods" -> UpdatePaths.MODS_DIR;
            case "shaderpacks" -> UpdatePaths.SHADERPACKS_DIR;
            case "resourcepacks" -> UpdatePaths.RESOURCEPACKS_DIR;
            default -> throw new IllegalArgumentException("未知内容类型：" + kind);
        };
    }

    /** 内容类型 → 中文名称（清单失败提示等文案用）。 */
    private static String labelForKind(String kind) {
        return switch (kind) {
            case "mods" -> "模组";
            case "shaderpacks" -> "光影包";
            case "resourcepacks" -> "资源包";
            default -> kind;
        };
    }

    private Map<String, String> fetchManifest(String kind) {
        try {
            String raw = UpdateNetwork.tcpCommand(config.host, config.tcpPort,
                    "GET_MANIFEST|" + kind, MANIFEST_TIMEOUT_MS, activeSocket);
            JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
            Map<String, String> manifest = new LinkedHashMap<>();
            for (Map.Entry<String, JsonElement> e : obj.entrySet()) {
                manifest.put(e.getKey(), e.getValue().getAsString());
            }
            SpeedUpdate.LOGGER.info("[云更新] 获取 {} 清单：{} 个文件", kind, manifest.size());
            return manifest;
        } catch (Exception e) {
            SpeedUpdate.LOGGER.error("[云更新] 获取 {} 清单失败：{}", kind, e.toString());
            if (!cancelled.get()) {
                fail("网络异常，无法获取" + labelForKind(kind) + "清单，请检查网络后重试");
            }
            return null;
        }
    }

    /** 扫描本地目录，返回 相对路径(以游戏目录为基准) -> sha1。
     *  onlyZako=true（正常更新）：只统计文件名含 _ZAKO 标识的云端文件；玩家自装文件不纳入管理。
     *  onlyZako=false（修复模式）：按扩展名统计全部文件，严格对齐云端。 */
    private Map<String, String> scanLocal(Path dir, String ext, boolean onlyZako) {
        Map<String, String> result = new LinkedHashMap<>();
        if (!Files.isDirectory(dir)) {
            return result;
        }
        try {
            collectFiles(dir, result, ext, onlyZako);
        } catch (IOException e) {
            SpeedUpdate.LOGGER.warn("[云更新] 扫描本地目录 {} 失败：{}", dir, e.toString());
        }
        return result;
    }

    private void collectFiles(Path dir, Map<String, String> out, String ext, boolean onlyZako) throws IOException {
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir)) {
            for (Path p : stream) {
                if (cancelled.get()) {
                    return;
                }
                if (Files.isDirectory(p)) {
                    collectFiles(p, out, ext, onlyZako);
                } else if (Files.isRegularFile(p)) {
                    String name = p.getFileName().toString();
                    // 只识别并处理指定扩展名的文件；其余文件/文件夹一律忽略（防止误删 mod 自建的文件）
                    if (!name.toLowerCase(Locale.ROOT).endsWith(ext)) {
                        continue;
                    }
                    // 正常更新：只认云端身份标识 _ZAKO 的文件（玩家自装 = 自己负责，云端永不处理）
                    if (onlyZako && !name.toUpperCase(Locale.ROOT).contains("_ZAKO")) {
                        continue;
                    }
                    String rel = UpdatePaths.GAME_DIR.relativize(p).toString().replace('\\', '/');
                    try {
                        out.put(rel, sha1(p));
                    } catch (IOException e) {
                        SpeedUpdate.LOGGER.warn("[云更新] 计算 {} 哈希失败：{}", rel, e.toString());
                    }
                }
            }
        }
    }

    /**
     * 单哈希验证（与文件名无关）：本地只要存在内容相同（SHA-1 一致）的文件，
     * 就视为该内容已安装，无需下载——服务端改名/换文件名不会触发全量下载。
     */
    private List<Map.Entry<String, String>> diffToDownload(Map<String, String> remote, Map<String, String> local) {
        Set<String> localHashes = new HashSet<>();
        for (String h : local.values()) {
            localHashes.add(h.toLowerCase(Locale.ROOT));
        }
        List<Map.Entry<String, String>> list = new ArrayList<>();
        for (Map.Entry<String, String> e : remote.entrySet()) {
            if (!localHashes.contains(e.getValue().toLowerCase(Locale.ROOT))) {
                list.add(e);
            }
        }
        return list;
    }

    /**
     * 单哈希验证：本地文件的内容（SHA-1）已不存在于服务端时删除；
     * 内容仍在（只是文件名不同）的一律保留，避免误删改名后的旧文件。
     */
    private List<String> diffToDelete(Map<String, String> remote, Map<String, String> local) {
        Set<String> remoteHashes = new HashSet<>();
        for (String h : remote.values()) {
            remoteHashes.add(h.toLowerCase(Locale.ROOT));
        }
        List<String> list = new ArrayList<>();
        for (Map.Entry<String, String> entry : local.entrySet()) {
            if (!remoteHashes.contains(entry.getValue().toLowerCase(Locale.ROOT))) {
                list.add(entry.getKey());
            }
        }
        return list;
    }

    private static String sha1(Path file) throws IOException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-1");
        } catch (Exception e) {
            throw new IOException("SHA-1 算法不可用", e);
        }
        byte[] buf = new byte[64 * 1024];
        try (InputStream in = Files.newInputStream(file)) {
            int n;
            while ((n = in.read(buf)) != -1) {
                digest.update(buf, 0, n);
            }
        }
        StringBuilder sb = new StringBuilder(40);
        for (byte b : digest.digest()) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    private static void moveReplace(Path from, Path to) throws IOException {
        try {
            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException atomicFailure) {
            // 跨卷 / 不支持原子移动时退回普通移动
            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private void clearCacheDirectories() {
        clearCacheDir(UpdatePaths.CACHE_MODS_DIR);
        clearCacheDir(UpdatePaths.CACHE_SHADERPACKS_DIR);
        clearCacheDir(UpdatePaths.CACHE_RESOURCEPACKS_DIR);
    }

    private void clearCacheDir(Path dir) {
        try {
            if (!Files.isDirectory(dir)) {
                Files.createDirectories(dir);
                return;
            }
            try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir)) {
                for (Path p : stream) {
                    deleteRecursively(p);
                }
            }
        } catch (IOException e) {
            SpeedUpdate.LOGGER.warn("[云更新] 清空缓存目录 {} 失败：{}", dir, e.toString());
        }
    }

    private static void deleteRecursively(Path path) throws IOException {
        if (Files.isDirectory(path)) {
            try (DirectoryStream<Path> stream = Files.newDirectoryStream(path)) {
                for (Path p : stream) {
                    deleteRecursively(p);
                }
            }
        }
        Files.deleteIfExists(path);
    }

    /** 可中断的等待（取消时立即返回）。 */
    private void sleepInterruptibly(long millis) {
        CountDownLatch latch = new CountDownLatch(1);
        sleepLatch = latch;
        try {
            latch.await(millis, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void closeQuietly(Socket socket) {
        if (socket != null) {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
        }
    }
}
