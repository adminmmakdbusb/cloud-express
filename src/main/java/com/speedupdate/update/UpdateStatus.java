package com.speedupdate.update;

/**
 * 客户端全局更新状态（供主界面「检查更新」按钮显示绿色提示）。
 *
 * 写入点：
 *  - 启动静默检查（SpeedUpdateClient）发现新版本时置 true
 *  - 更新流程完成（UpdateEngine 进入 DONE）时重置 false，按钮恢复默认样式
 * 读取点：
 *  - TitleScreenMixin 每帧通过 Supplier 读取，按钮文字/颜色自动刷新
 */
public final class UpdateStatus {
    private UpdateStatus() {
    }

    /** 启动静默检查发现新版本时为 true；更新完成后重置为 false。 */
    public static volatile boolean newVersionAvailable = false;
}
