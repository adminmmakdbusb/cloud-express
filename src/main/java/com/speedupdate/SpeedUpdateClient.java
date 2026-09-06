package com.speedupdate;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.speedupdate.update.ServerConfig;
import com.speedupdate.update.UpdateNetwork;
import com.speedupdate.update.UpdatePaths;
import com.speedupdate.update.UpdateStatus;
import com.speedupdate.update.VersionNumber;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.event.lifecycle.FMLClientSetupEvent;

import java.net.Socket;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 客户端入口。
 * 本类不会在专用服务端加载，访问客户端代码是安全的。
 */
@Mod(value = SpeedUpdate.MODID, dist = Dist.CLIENT)
@EventBusSubscriber(modid = SpeedUpdate.MODID, value = Dist.CLIENT)
public class SpeedUpdateClient {

    public SpeedUpdateClient(ModContainer container) {
    }

    @SubscribeEvent
    static void onClientSetup(FMLClientSetupEvent event) {
        SpeedUpdate.LOGGER.info("[云更新] 客户端初始化完成，主界面将显示「检查更新」按钮");
        startSilentVersionCheck();
    }

    /**
     * 启动后静默向服务端请求一次版本号并与本地比对：
     * 发现新版本 → 主界面按钮显示绿色「有新版本·」；
     * 相同 / 更旧 / 连接失败 → 不做任何处理（按钮保持默认样式）。
     * 独立守护线程，10 秒超时，任何异常静默忽略，绝不影响游戏启动。
     */
    private static void startSilentVersionCheck() {
        Thread t = new Thread(SpeedUpdateClient::silentVersionCheck, "SpeedUpdate-SilentCheck");
        t.setDaemon(true);
        t.start();
    }

    private static void silentVersionCheck() {
        try {
            ServerConfig cfg = ServerConfig.load();
            AtomicReference<Socket> sock = new AtomicReference<>();
            String raw = UpdateNetwork.tcpCommand(cfg.host, cfg.tcpPort, "GET_VERSION", 10_000, sock);
            JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
            String remote = obj.has("version") ? obj.get("version").getAsString() : "";
            String local = UpdatePaths.readVersion();
            if (!remote.isBlank() && VersionNumber.parse(remote).isNewerThan(local)) {
                UpdateStatus.newVersionAvailable = true;
                SpeedUpdate.LOGGER.info("[云更新] 静默检查：发现新版本 {}（本地 {}），主界面按钮已点亮", remote, local);
            } else {
                SpeedUpdate.LOGGER.info("[云更新] 静默检查：已是最新（本地 {}，远程 {}）", local, remote);
            }
        } catch (Exception e) {
            SpeedUpdate.LOGGER.warn("[云更新] 静默检查失败（忽略）：{}", e.toString());
        }
    }
}
