package com.speedupdate.update;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.annotations.SerializedName;
import com.speedupdate.SpeedUpdate;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 服务端连接配置。
 *
 * 配置文件：config/speedupdate/server.json，首次启动自动创建：
 * {
 *   "host": "127.0.0.1",   // 服务端地址（测试阶段用 127.0.0.1，部署后可改为 frp 域名或服务器 IP）
 *   "tcpPort": 10086       // TCP 端口：版本/清单指令 + 文件下载，全部走这一个端口（默认 10086）
 * }
 *
 * 旧版配置中若存在 httpPort 字段会被自动忽略（Gson 跳过未知字段），
 * 配置文件损坏或缺失时使用默认值并自动重建，保证模组始终可用。
 */
public class ServerConfig {
    @SerializedName("host")
    public String host = "127.0.0.1";

    @SerializedName("tcpPort")
    public int tcpPort = 10086;

    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();

    private ServerConfig() {
    }

    /** 生成默认配置文件（仅当文件不存在时由启动初始化调用）。 */
    public static void saveDefault() {
        save(new ServerConfig());
    }

    public static ServerConfig load() {
        Path file = UpdatePaths.SERVER_CONFIG_FILE;
        if (Files.exists(file)) {
            try {
                String json = Files.readString(file, StandardCharsets.UTF_8);
                ServerConfig cfg = GSON.fromJson(json, ServerConfig.class);
                if (cfg != null && cfg.host != null && !cfg.host.isBlank()
                        && cfg.tcpPort > 0 && cfg.tcpPort < 65536) {
                    return cfg;
                }
                SpeedUpdate.LOGGER.warn("[云更新] server.json 内容不合法，使用默认配置");
            } catch (Exception e) {
                SpeedUpdate.LOGGER.warn("[云更新] 读取 server.json 失败：{}，使用默认配置", e.toString());
            }
        }
        ServerConfig cfg = new ServerConfig();
        save(cfg);
        return cfg;
    }

    public static void save(ServerConfig cfg) {
        try {
            Path parent = UpdatePaths.SERVER_CONFIG_FILE.getParent();
            if (parent != null && !Files.isDirectory(parent)) {
                Files.createDirectories(parent);
            }
            Files.writeString(UpdatePaths.SERVER_CONFIG_FILE, GSON.toJson(cfg), StandardCharsets.UTF_8);
        } catch (IOException e) {
            SpeedUpdate.LOGGER.warn("[云更新] 写入 server.json 失败：{}", e.toString());
        }
    }

    @Override
    public String toString() {
        return host + " (TCP:" + tcpPort + ")";
    }
}
