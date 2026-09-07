package com.speedupdate;

import com.mojang.logging.LogUtils;
import com.speedupdate.update.UpdatePaths;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.event.lifecycle.FMLCommonSetupEvent;
import net.neoforged.fml.loading.FMLEnvironment;
import org.slf4j.Logger;

/**
 * 整合包云更新 - 主类。
 *
 * 功能：
 *  - 启动早期创建 speedmod_cache / config 等目录与 JSON 文件
 *  - FMLCommonSetupEvent 中（enqueueWork）执行「重启后删除旧文件」任务
 *  - 客户端：主界面「检查更新」按钮 + 更新窗口（见 SpeedUpdateClient / UpdateScreen）
 */
@Mod(SpeedUpdate.MODID)
public class SpeedUpdate {
    public static final String MODID = "speedupdate";
    public static final Logger LOGGER = LogUtils.getLogger();

    public SpeedUpdate(IEventBus modEventBus, ModContainer modContainer) {
        LOGGER.info("[云更新] 模组加载开始");
        // 模组加载最早时机（构造方法）：确保目录与 JSON 文件存在。
        // 仅客户端需要这些 .minecraft 目录；服务端安装本模组时跳过。
        if (FMLEnvironment.getDist() == Dist.CLIENT) {
            UpdatePaths.ensureDirectories();
        }
        modEventBus.addListener(this::commonSetup);
    }

    private void commonSetup(FMLCommonSetupEvent event) {
        if (FMLEnvironment.getDist() != Dist.CLIENT) {
            return;
        }
        // 重启后处理待删除任务（删除远程清单中已不存在的旧文件）：游戏在此阶段冻结，等待完成才继续加载。
        // 无论处理成功与否都会清空待办列表，防止每次启动重复尝试导致卡死。
        event.enqueueWork(UpdatePaths::processPendingTasks);
        LOGGER.info("[云更新] 启动初始化完成，待删除文件处理任务已提交");
    }
}
