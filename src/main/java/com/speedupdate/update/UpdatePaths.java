package com.speedupdate.update;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.speedupdate.SpeedUpdate;
import net.neoforged.fml.loading.FMLPaths;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

/**
 * 客户端目录与 JSON 文件管理。
 *
 * .minecraft/
 * ├── mods/                           正式模组目录（只处理 .jar）
 * ├── shaderpacks/                    正式光影包目录（只处理 .zip）
 * ├── resourcepacks/                  正式资源包目录（只处理 .zip）
 * ├── speedmod_cache/                 临时下载缓存
 * │   ├── mods/
 * │   ├── shaderpacks/
 * │   └── resourcepacks/
 * └── config/
 *     └── speedupdate/
 *         ├── Versionnumber.json      当前整合包版本 {"version": "1.0.000", "feedbackUrl": "…"}
 *         ├── pending_deletions.json  待删除文件列表 ["mods/xxx.jar", ...]
 *         ├── server.json             服务端地址配置（首次运行自动生成）
 *         └── server.py               服务端程序（首次启动从 mod 释放，随整合包一起分发）
 */
public final class UpdatePaths {

    private UpdatePaths() {
    }

    public static final Path GAME_DIR = FMLPaths.GAMEDIR.get();
    public static final Path MODS_DIR = GAME_DIR.resolve("mods");
    public static final Path SHADERPACKS_DIR = GAME_DIR.resolve("shaderpacks");
    public static final Path RESOURCEPACKS_DIR = GAME_DIR.resolve("resourcepacks");
    public static final Path CACHE_DIR = GAME_DIR.resolve("speedmod_cache");
    public static final Path CACHE_MODS_DIR = CACHE_DIR.resolve("mods");
    public static final Path CACHE_SHADERPACKS_DIR = CACHE_DIR.resolve("shaderpacks");
    public static final Path CACHE_RESOURCEPACKS_DIR = CACHE_DIR.resolve("resourcepacks");
    // 配置统一存放于 config/speedupdate（早期曾用 config/speedmod，发布版直接使用新目录，无需迁移旧数据）
    public static final Path CONFIG_DIR = GAME_DIR.resolve("config").resolve("speedupdate");
    public static final Path VERSION_FILE = CONFIG_DIR.resolve("Versionnumber.json");
    public static final Path PENDING_FILE = CONFIG_DIR.resolve("pending_deletions.json");
    public static final Path SERVER_CONFIG_FILE = GAME_DIR.resolve("config").resolve("speedupdate").resolve("server.json");

    public static final String DEFAULT_VERSION = "1.0.000";
    /** 配置缺省 / 云端未推送时的「问题反馈」链接。 */
    public static final String DEFAULT_FEEDBACK_URL = "https://chat.deepseek.com/";

    /** 启动时确保目录与 JSON 文件存在（已存在则不覆盖）。 */
    public static void ensureDirectories() {
        try {
            mkdirs(MODS_DIR, SHADERPACKS_DIR, RESOURCEPACKS_DIR,
                    CACHE_MODS_DIR, CACHE_SHADERPACKS_DIR, CACHE_RESOURCEPACKS_DIR, CONFIG_DIR);
            if (!Files.exists(VERSION_FILE)) {
                writeVersion(DEFAULT_VERSION);
                SpeedUpdate.LOGGER.info("[云更新] 已创建 Versionnumber.json（初始版本 {}）", DEFAULT_VERSION);
            }
            if (!Files.exists(PENDING_FILE)) {
                writePending(List.of());
                SpeedUpdate.LOGGER.info("[云更新] 已创建 pending_deletions.json（空列表）");
            }
            if (!Files.exists(SERVER_CONFIG_FILE)) {
                ServerConfig.saveDefault();
                SpeedUpdate.LOGGER.info("[云更新] 已创建 server.json（默认 127.0.0.1:10086）");
            }
            ensureServerTemplate();  // 释放 jar 内置服务端程序（仅首次，已存在不覆盖）
            SpeedUpdate.LOGGER.info("[云更新] 目录初始化完成：mods={} shaderpacks={} resourcepacks={} cache={}",
                    Files.exists(MODS_DIR), Files.exists(SHADERPACKS_DIR),
                    Files.exists(RESOURCEPACKS_DIR), Files.exists(CACHE_DIR));
        } catch (IOException e) {
            SpeedUpdate.LOGGER.error("[云更新] 目录初始化失败", e);
        }
    }

    private static void mkdirs(Path... dirs) throws IOException {
        for (Path d : dirs) {
            if (!Files.isDirectory(d)) {
                Files.createDirectories(d);
            }
        }
    }

    /**
     * 把 jar 内置的服务端程序（resources/server/server.py）释放到 config/speedupdate/server.py：
     * 仅当目标不存在时原样字节拷贝（存在不覆盖——作者可能已部署或自行修改，升级服务端请删除旧文件后重启）。
     */
    public static void ensureServerTemplate() {
        Path target = CONFIG_DIR.resolve("server.py");
        if (Files.exists(target)) {
            SpeedUpdate.LOGGER.info("[云更新] 检测到已有服务端文件 {}，跳过释放（需要新版请删除该文件后重启）", target);
            return;
        }
        try (InputStream in = SpeedUpdate.class.getResourceAsStream("/server/server.py")) {
            if (in == null) {
                SpeedUpdate.LOGGER.warn("[云更新] jar 内未找到服务端模板 /server/server.py，跳过释放");
                return;
            }
            Files.createDirectories(target.getParent());
            Files.copy(in, target);
            SpeedUpdate.LOGGER.info("[云更新] 已从 mod 释放服务端文件：{}", target);
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 释放服务端文件失败（不影响其它功能）：{}", e.toString());
        }
    }

    /** 读取当前版本号；文件缺失或损坏时回退到默认版本并修复文件。 */
    public static String readVersion() {
        try {
            if (!Files.exists(VERSION_FILE)) {
                writeVersion(DEFAULT_VERSION);
                return DEFAULT_VERSION;
            }
            JsonObject obj = JsonParser.parseString(Files.readString(VERSION_FILE, StandardCharsets.UTF_8)).getAsJsonObject();
            String v = obj.has("version") ? obj.get("version").getAsString() : "";
            return v.isEmpty() ? DEFAULT_VERSION : v;
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 读取 Versionnumber.json 失败，使用默认版本", e);
            try {
                writeVersion(DEFAULT_VERSION);
            } catch (Exception ignored) {
            }
            return DEFAULT_VERSION;
        }
    }

    /** 写入版本号（读改写：保留既有 feedbackUrl 字段，防止云端推送的链接被覆盖）。 */
    public static void writeVersion(String version) throws IOException {
        JsonObject obj = readVersionJson();
        obj.addProperty("version", version);
        writeVersionJson(obj);
    }

    /** 读取「问题反馈」链接；文件缺失 / 无该字段 / 损坏时回退默认值。 */
    public static String readFeedbackUrl() {
        try {
            JsonObject obj = readVersionJson();
            if (obj.has("feedbackUrl")) {
                String url = obj.get("feedbackUrl").getAsString();
                if (url != null && !url.isBlank()) {
                    return url;
                }
            }
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 读取反馈链接失败，使用默认值", e);
        }
        return DEFAULT_FEEDBACK_URL;
    }

    /** 写入「问题反馈」链接（云端推送后调用；保留 version 字段）。 */
    public static void writeFeedbackUrl(String url) throws IOException {
        JsonObject obj = readVersionJson();
        obj.addProperty("feedbackUrl", url);
        writeVersionJson(obj);
    }

    /** 读取 Versionnumber.json 的 JSON 对象；不存在 / 损坏时返回空对象。 */
    private static JsonObject readVersionJson() {
        try {
            if (Files.exists(VERSION_FILE)) {
                JsonElement el = JsonParser.parseString(Files.readString(VERSION_FILE, StandardCharsets.UTF_8));
                if (el.isJsonObject()) {
                    return el.getAsJsonObject();
                }
            }
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 读取 Versionnumber.json 失败，按空对象处理", e);
        }
        return new JsonObject();
    }

    /** 写 Versionnumber.json（UTF-8 覆盖写）。 */
    private static void writeVersionJson(JsonObject obj) throws IOException {
        Files.createDirectories(VERSION_FILE.getParent());
        Files.writeString(VERSION_FILE, obj.toString(), StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
    }

    /** 读取待删除文件列表（相对 .minecraft 的路径，如 mods/xxx.jar）。 */
    public static List<String> readPending() {
        try {
            if (!Files.exists(PENDING_FILE)) {
                return new ArrayList<>();
            }
            JsonElement el = JsonParser.parseString(Files.readString(PENDING_FILE, StandardCharsets.UTF_8));
            List<String> list = new ArrayList<>();
            if (el.isJsonArray()) {
                JsonArray arr = el.getAsJsonArray();
                for (JsonElement item : arr) {
                    if (item.isJsonPrimitive()) {
                        String s = item.getAsString();
                        if (s != null && !s.isEmpty()) {
                            list.add(s);
                        }
                    }
                }
            }
            return list;
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 读取 pending_deletions.json 失败，视为空列表", e);
            return new ArrayList<>();
        }
    }

    /** 写入待删除文件列表。 */
    public static void writePending(List<String> paths) throws IOException {
        JsonArray arr = new JsonArray();
        for (String p : paths) {
            arr.add(p);
        }
        Files.createDirectories(PENDING_FILE.getParent());
        Files.writeString(PENDING_FILE, arr.toString(), StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
    }

    /**
     * 重启后处理待删除任务：删除远程清单中已不存在的旧文件。
     *
     * 处理策略（Windows 下 mod 的 jar 此刻已被游戏加载，可能被锁定）：
     *  - 删除后必须验证文件确实消失，才记成功日志（绝不无脑输出成功）
     *  - 文件已不存在（可能被玩家手动删过）→ 记日志并直接移除条目
     *  - 删除失败（被游戏/杀毒软件占用）→ 短重试 3 次，仍失败则登记
     *    JVM 退出时删除（File.deleteOnExit，游戏退出后、下次启动前生效），
     *    且条目保留在 pending_deletions.json 中供下次启动继续重试
     *  - 非法路径条目直接移除
     * 只有「未删除成功」的条目才会保留，其余一律清理。
     */
    public static void processPendingTasks() {
        processDeletions();
    }

    private static void processDeletions() {
        List<String> pending = readPending();
        if (pending.isEmpty()) {
            return;
        }
        SpeedUpdate.LOGGER.info("[云更新] 发现待删除文件 {} 个，开始处理", pending.size());
        List<String> remain = new ArrayList<>();
        int deleted = 0;
        int absent = 0;
        int failed = 0;
        for (String rel : pending) {
            Path target = GAME_DIR.resolve(rel).normalize();
            // 安全校验：只允许删除 mods/、shaderpacks/ 或 resourcepacks/ 下的文件，防止异常数据误删其他文件
            if (!target.startsWith(GAME_DIR)
                    || !(target.startsWith(MODS_DIR) || target.startsWith(SHADERPACKS_DIR) || target.startsWith(RESOURCEPACKS_DIR))) {
                SpeedUpdate.LOGGER.warn("[云更新] 跳过非法待删除路径：{}", rel);
                continue;
            }
            if (!Files.exists(target)) {
                absent++;
                SpeedUpdate.LOGGER.info("[云更新] 跳过删除：文件已不存在（可能已手动删除）：{}", rel);
                continue;
            }
            // 文件可能被游戏 / 杀毒软件短暂占用：重试 3 次（间隔 500ms）
            boolean deletedNow = false;
            for (int attempt = 1; attempt <= 3 && !deletedNow; attempt++) {
                try {
                    deletedNow = Files.deleteIfExists(target) && !Files.exists(target);
                } catch (IOException e) {
                    SpeedUpdate.LOGGER.warn("[云更新] 删除文件失败（第 {} 次尝试，文件被占用）：{} - {}",
                            attempt, rel, e.getMessage());
                }
                if (!deletedNow && attempt < 3) {
                    try {
                        Thread.sleep(500);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
            }
            if (deletedNow) {
                deleted++;
                SpeedUpdate.LOGGER.info("[云更新] 已删除旧文件：{}", rel);
            } else {
                failed++;
                // 删除被占用：登记 JVM 退出时删除（游戏退出后生效），条目保留供下次启动重试
                try {
                    target.toFile().deleteOnExit();
                } catch (Exception ignored) {
                }
                SpeedUpdate.LOGGER.warn("[云更新] 删除失败（文件被占用），已登记游戏退出时删除，条目保留下次启动重试：{}", rel);
                remain.add(rel);
            }
        }
        // 只保留「未删除成功」的条目；成功 / 已不存在 / 非法条目一律移除
        try {
            writePending(remain);
        } catch (IOException e) {
            SpeedUpdate.LOGGER.warn("[云更新] 写入 pending_deletions.json 失败", e);
        }
        SpeedUpdate.LOGGER.info("[云更新] 待删除文件处理完成：成功 {} 个，跳过 {} 个（已不存在），失败 {} 个（保留下次重试）",
                deleted, absent, failed);
    }
}
