package com.speedupdate.gui;

import com.speedupdate.SpeedUpdate;
import com.speedupdate.update.UpdateEngine;
import com.speedupdate.update.UpdateEngine.Phase;
import com.speedupdate.update.UpdatePaths;
import net.minecraft.util.Util;
import net.minecraft.client.input.KeyEvent;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.Font;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.components.Renderable;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.Identifier;
import net.minecraft.util.FormattedCharSequence;

import java.util.List;
import java.util.Locale;

/**
 * 更新检查窗口：315x225 模态面板，7 种状态
 * （请求中 / 网络异常 / 有新版本 / 已是最新 / 下载中 / 更新完成 / 更新失败）。
 *
 * UI：晴空深色主题（圆角卡片、语义色胶囊按钮、纯文本更新日志卡片）。
 * 动画克制（原版 GUI 能力内）：仅请求中旋转加载圈 + 更新完成描边勾。
 * 实时进度：文件数加权 + 文件内字节比例，下载中页面显示
 * 「已下载 / 文件大小」与实时速度（坐标固定，文件名超长截断省略）。
 *
 * 窗口打开立即 GET_VERSION（10 秒超时）；所有网络操作在独立线程，
 * UI 每帧直接读取 engine 的 volatile 状态字段。
 * 下载中禁用 ESC 关闭；「取消下载」断开连接并清空缓存目录
 * （已同步到正式目录的文件保留，版本号不写入）。
 */
public class UpdateScreen extends Screen {

    private static final int PANEL_W = 315;
    private static final int PANEL_H = 225;

    /** 模组图标（jar 内 icon.png，256x256，缩放到 19x19 显示）。 */
    private static final Identifier ICON = Identifier.fromNamespaceAndPath(SpeedUpdate.MODID, "textures/icon.png");
    /** 「修复客户端」按钮图标（assets/speedupdate/textures/xiufu.png，256x256）。 */
    private static final Identifier XIUFU = Identifier.fromNamespaceAndPath(SpeedUpdate.MODID, "textures/xiufu.png");

    private final UpdateEngine engine = new UpdateEngine();
    private boolean checkStarted = false;
    private int panelX;
    private int panelY;

    /** 更新日志滚动偏移（像素，仅「有新版本」阶段使用，渲染时夹取到合法范围）。 */
    private double changelogScroll = 0;
    /** 更新完成勾动画开始时间戳；离开 DONE 状态时归零，重新进入时重新播放。 */
    private long doneAnimStart = 0;

    private ModWidgets.ActionButton retryButton;         // ERROR / FAILED
    private ModWidgets.ActionButton updateButton;        // UPDATE_AVAILABLE
    private ModWidgets.ActionButton laterButton;         // UPDATE_AVAILABLE「稍后再说」
    private ModWidgets.ActionButton feedbackButton;      // UP_TO_DATE
    private ModWidgets.ActionButton cancelButton;        // ERROR / UP_TO_DATE / FAILED「取消」
    private ModWidgets.ActionButton cancelDownloadButton;// DOWNLOADING「取消下载」
    private ModWidgets.ActionButton restartButton;       // DONE「立即重启」
    private ModWidgets.ActionButton laterDoneButton;     // DONE「稍后再说」
    private ModWidgets.IconButton repairIconButton;      // 顶栏「修复客户端」锤子（仅 有新版本/已是最新 两页显示）
    private ModWidgets.ActionButton repairStartButton;   // 修复确认页「开始修复」（红，左）
    private ModWidgets.ActionButton repairCancelButton;  // 修复确认页「取消」（右，与其它页右取消统一）

    /** true=正在显示「修复客户端」确认页（锤子点击后，顶栏保留）。 */
    private boolean repairConfirming = false;

    public UpdateScreen() {
        super(Component.literal("更新检查"));
    }

    @Override
    protected void init() {
        this.panelX = (this.width - PANEL_W) / 2;
        this.panelY = (this.height - PANEL_H) / 2;
        this.clearWidgets();
        this.repairConfirming = false;  // 界面重建（缩放/重开）时退出确认态
        int cx = panelX + PANEL_W / 2;
        int by = panelY + 194;
        int bxL = cx - 124;
        int bxR = cx + 4;
        int bxC = cx - 60;

        this.retryButton = new ModWidgets.ActionButton(bxL, by, 120, 20, "重试",
                ModStyle.BTN_BLUE, ModStyle.BTN_BLUE_TEXT, () -> {
            // 网络异常 → 重新请求版本号；更新失败 → 沿用原模式重试（修复失败仍按修复重试）
            if (engine.phase == Phase.FAILED) {
                engine.startRetry();
            } else {
                engine.checkVersion();
            }
        }, () -> true);
        this.updateButton = new ModWidgets.ActionButton(bxL, by, 120, 20, "立刻更新",
                ModStyle.BTN_PRIMARY, ModStyle.BTN_PRIMARY_TEXT, engine::startUpdate);
        this.laterButton = new ModWidgets.ActionButton(bxR, by, 120, 20, "稍后再说",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::onClose, () -> true);
        this.feedbackButton = new ModWidgets.ActionButton(bxL, by, 120, 20, "反馈问题",
                ModStyle.BUTTON_BG, ModStyle.TEXT, true,
                // 链接实时读取本地配置（云端可在更新后推送更改）
                () -> Util.getPlatform().openUri(UpdatePaths.readFeedbackUrl()), () -> true);
        this.cancelButton = new ModWidgets.ActionButton(bxR, by, 120, 20, "取消",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::onClose, () -> true);
        this.cancelDownloadButton = new ModWidgets.ActionButton(bxC, by, 120, 20, "取消下载",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::cancelDownload, () -> true);
        this.restartButton = new ModWidgets.ActionButton(bxR, by, 120, 20, "立即重启",
                ModStyle.BTN_ORANGE, ModStyle.BTN_ORANGE_TEXT, () -> Minecraft.getInstance().stop());
        this.laterDoneButton = new ModWidgets.ActionButton(bxL, by, 120, 20, "稍后再说",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::onClose, () -> true);

        // 「修复客户端」确认页按钮（页面打开时由 syncButtons 控制可见；开始修复在左、取消在右）
        this.repairStartButton = new ModWidgets.ActionButton(bxL, by, 120, 20, "开始修复",
                ModStyle.BTN_RED, ModStyle.BTN_RED_TEXT, () -> {
            repairConfirming = false;
            syncButtons();
            engine.startRepair();   // 严格对齐云端：复用完整更新流程（含写版本号等一切）
        }, () -> true);
        this.repairCancelButton = new ModWidgets.ActionButton(bxR, by, 120, 20, "取消",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::closeRepairConfirm, () -> true);
        // 顶栏锤子入口（位置在 render() 中按本地版本文本动态计算）
        this.repairIconButton = new ModWidgets.IconButton(0, 0, 19, XIUFU, 256,
                () -> {
                    repairConfirming = true;
                    syncButtons();
                },
                () -> engine.phase == Phase.UPDATE_AVAILABLE || engine.phase == Phase.UP_TO_DATE);

        addRenderableWidget(retryButton);
        addRenderableWidget(updateButton);
        addRenderableWidget(laterButton);
        addRenderableWidget(feedbackButton);
        addRenderableWidget(cancelButton);
        addRenderableWidget(cancelDownloadButton);
        addRenderableWidget(restartButton);
        addRenderableWidget(laterDoneButton);
        addRenderableWidget(repairIconButton);
        addRenderableWidget(repairStartButton);
        addRenderableWidget(repairCancelButton);

        engine.setListener(this::onEngineStateChanged);
        syncButtons();
        if (!checkStarted) {
            checkStarted = true;
            engine.checkVersion(); // 打开即自动请求版本号
        }
    }

    /** 引擎状态变化（主线程回调）：刷新按钮可见性；离开对应状态时重置动画/滚动。 */
    private void onEngineStateChanged() {
        if (this.minecraft != null && this.minecraft.screen == this) {
            if (engine.phase != Phase.UPDATE_AVAILABLE) {
                changelogScroll = 0;
            }
            if (engine.phase != Phase.DONE) {
                doneAnimStart = 0;
            }
            syncButtons();
        }
    }

    private void syncButtons() {
        Phase p = engine.phase;
        boolean confirm = repairConfirming;
        // 「修复确认页」打开时隐藏全部常规按钮，避免与确认按钮交叉冲突
        retryButton.visible = !confirm && (p == Phase.ERROR || p == Phase.FAILED);
        updateButton.visible = !confirm && p == Phase.UPDATE_AVAILABLE;
        laterButton.visible = !confirm && p == Phase.UPDATE_AVAILABLE;
        feedbackButton.visible = !confirm && p == Phase.UP_TO_DATE;
        cancelButton.visible = !confirm && (p == Phase.ERROR || p == Phase.UP_TO_DATE || p == Phase.FAILED);
        cancelDownloadButton.visible = !confirm && p == Phase.DOWNLOADING;
        restartButton.visible = !confirm && p == Phase.DONE;
        laterDoneButton.visible = !confirm && p == Phase.DONE;
        // 顶栏锤子：只在 有新版本 / 已是最新 两页显示（其余状态隐藏，避免逻辑交叉）
        repairIconButton.visible = !confirm && (p == Phase.UPDATE_AVAILABLE || p == Phase.UP_TO_DATE);
        // 修复确认页按钮
        repairStartButton.visible = confirm;
        repairCancelButton.visible = confirm;
    }

    /** 取消修复确认：回到原页面（点「取消」或按 ESC）。 */
    private void closeRepairConfirm() {
        repairConfirming = false;
        syncButtons();
    }

    /** 「取消下载」：断开连接并清空缓存目录（已同步到正式目录的文件保留），然后关闭窗口。 */
    private void cancelDownload() {
        engine.cancelDownloadAndClean();
        onClose();
    }

    @Override
    public void onClose() {
        // 清理：注销回调、中止所有网络请求、断开连接、清空临时状态
        engine.setListener(null);
        engine.cancel();
        super.onClose();
    }

    @Override
    public boolean shouldCloseOnEsc() {
        // 下载过程中禁止 ESC 关闭，防止打断更新流程
        return engine.phase != Phase.DOWNLOADING;
    }

    @Override
    public void extractRenderState(GuiGraphicsExtractor g, int mouseX, int mouseY, float partialTick) {
        // 全屏暗色遮罩（模态感）
        g.fill(0, 0, this.width, this.height, ModStyle.OVERLAY);

        // 面板
        ModStyle.drawPanel(g, panelX, panelY, PANEL_W, PANEL_H, 8);
        Font font = this.font;

        // 顶栏：模组图标 + 标题 + 本地版本
        try {
            // 缩放重载：整张 256x256 图标等比缩放到 19x19（裁剪重载会只显示左上角透明区导致镂空图案不可见）
            g.blit(ICON, panelX + 12, panelY + 9, 19, 19, 0f, 0f, 1f, 1f);
        } catch (Exception ignored) {
            // 图标加载失败（极端情况）时仅显示文字标题，不影响功能
        }
        g.text(font, "更新检查", panelX + 36, panelY + 14, ModStyle.TEXT);
        String localText = "本地 v" + (engine.localVersion.isEmpty() ? "…" : engine.localVersion);
        int textX = panelX + PANEL_W - 12 - font.width(localText);
        g.text(font, localText, textX, panelY + 14, ModStyle.TEXT_FAINT);
        // 「修复客户端」锤子按钮：贴在本地版本号左侧，19x19 与左上 logo 同一水平高度
        repairIconButton.setX(textX - 4 - 19);
        repairIconButton.setY(panelY + 9);
        ModStyle.drawHairline(g, panelX + 12, panelX + PANEL_W - 12, panelY + 32);

        renderContent(g, font);

        // 按钮最后渲染（最上层）
        for (Renderable renderable : this.renderables) {
            renderable.extractRenderState(g, mouseX, mouseY, partialTick);
        }
    }

    private void renderContent(GuiGraphicsExtractor g, Font font) {
        int cx = panelX + PANEL_W / 2;
        int top = panelY + 40;
        // 「修复客户端」确认页（顶栏保留，内容区整体替换；按钮由 widgets 渲染）
        if (repairConfirming) {
            renderRepairConfirm(g, font, cx);
            return;
        }
        switch (engine.phase) {
            case REQUESTING -> {
                ModStyle.drawSpinner(g, cx, panelY + 82, 9f, System.currentTimeMillis());
                drawBigCentered(g, font, "请求中...", cx, panelY + 102, ModStyle.ACCENT, 1.25f);
                drawCentered(g, font, "正在连接服务器...", cx, panelY + 122, ModStyle.TEXT_FAINT);
                drawCentered(g, font, "服务器：" + engine.getConfig(), cx, panelY + 136, ModStyle.TEXT_FAINT);
            }
            case ERROR -> {
                drawBigCentered(g, font, "网络异常", cx, panelY + 78, ModStyle.RED, 1.3f);
                drawCentered(g, font, "无法连接服务器", cx, panelY + 106, ModStyle.RED);
                drawCentered(g, font, "请检查网络后重试", cx, panelY + 120, ModStyle.TEXT_FAINT);
            }
            case UPDATE_AVAILABLE -> {
                drawBigCentered(g, font, "有新版本可更新！", cx, top - 2, ModStyle.ORANGE, 1.15f);
                drawCentered(g, font, "v" + engine.remoteVersion, cx, top + 8, ModStyle.ACCENT);
                String log = engine.changelog;
                if (log == null || log.isBlank()) {
                    drawCentered(g, font, "（本次更新暂无日志说明）", cx, top + 20, ModStyle.TEXT_FAINT);
                } else {
                    renderChangelog(g, font, log);
                }
            }
            case UP_TO_DATE -> {
                drawBigCentered(g, font, "已是最新版本！", cx, panelY + 86, ModStyle.GREEN, 1.3f);
                drawCentered(g, font, "当前版本：v" + engine.localVersion, cx, panelY + 118, ModStyle.TEXT_FAINT);
            }
            case DOWNLOADING -> {
                int x0 = panelX + 12;
                int y0 = top + 0;
                // 阶段徽标 + 状态行（贴内容区顶部）：按当前文件前缀区分 模组 / 光影包 / 资源包
                String tag = engine.currentFile.startsWith("shaderpacks/") ? "光影包"
                        : engine.currentFile.startsWith("resourcepacks/") ? "资源包"
                        : "模组";
                int tagW = font.width(tag) + 12;
                ModStyle.fillRoundedRect(g, x0, y0, tagW, 12, 6, 0x2238BDF8);
                g.text(font, tag, x0 + 6, y0 + 2, ModStyle.ACCENT);
                g.text(font, engine.statusLine.isEmpty() ? "准备中…" : engine.statusLine,
                        x0 + tagW + 6, y0 + 2, ModStyle.TEXT_FAINT);
                // 标题（紧贴徽标下方）
                drawBigCentered(g, font, "正在更新...", cx, y0 + 19, ModStyle.TEXT, 1.2f);
                // 文件行：文件名固定宽度截断；MB 与速度坐标固定不变
                int fy = y0 + 36;
                g.text(font, trimToWidth(font, engine.currentFile, 135), x0, fy, ModStyle.TEXT_DIM);
                String mbText = fmtSize(engine.currentDownloaded) + " / " + fmtSize(engine.currentTotal);
                g.text(font, mbText, x0 + 142, fy, ModStyle.TEXT);
                String spd = fmtSpeed(engine.currentSpeedBps);
                g.text(font, spd, panelX + PANEL_W - 12 - font.width(spd), fy, ModStyle.ACCENT);
                // 进度条（贴文件行下方）+ 右侧百分比
                int barY = fy + 14;
                int barW = PANEL_W - 24 - 40;
                ModStyle.drawProgressBar(g, x0, barY, barW, 7, engine.progress, ModStyle.ACCENT);
                String pct = Math.round(engine.progress * 100) + "%";
                g.text(font, pct, panelX + PANEL_W - 12 - font.width(pct), barY - 1, ModStyle.TEXT_DIM);
                // 底部提示
                drawCentered(g, font, "下载中请勿关闭游戏", cx, barY + 16, ModStyle.TEXT_FAINT);
            }
            case DONE -> {
                if (doneAnimStart == 0) {
                    doneAnimStart = System.currentTimeMillis();
                }
                long now = System.currentTimeMillis();
                float ringT = Math.max(0f, Math.min(1f, (now - doneAnimStart) / 600f));
                float checkT = Math.max(0f, Math.min(1f, (now - doneAnimStart - 350) / 450f));
                ModStyle.drawAnimatedCheck(g, cx, panelY + 76, 34f, ringT, checkT);
                drawBigCentered(g, font, "更新完成！", cx, panelY + 108, ModStyle.GREEN, 1.3f);
                drawCentered(g, font, "已更新至 v" + engine.remoteVersion, cx, panelY + 134, ModStyle.TEXT_FAINT);
                if (engine.hasPendingDeletions) {
                    drawCentered(g, font, "部分文件需重启后生效", cx, panelY + 148, ModStyle.TEXT_FAINT);
                }
            }
            case FAILED -> {
                drawBigCentered(g, font, "更新失败", cx, panelY + 70, ModStyle.RED, 1.3f);
                String reason = engine.failReason.isEmpty() ? "更新过程发生未知错误，请重试" : engine.failReason;
                List<FormattedCharSequence> lines = font.split(Component.literal(reason), PANEL_W - 40);
                int y = panelY + 98;
                for (int i = 0; i < lines.size() && i < 2; i++) {
                    g.centeredText(font, lines.get(i), cx, y, ModStyle.TEXT);
                    y += font.lineHeight + 2;
                }
            }
        }
    }

    /**
     * 「修复客户端」确认页：红三角警告图标 + 红色标题 + 分级警告文案（居中排版）。
     * 按钮（开始修复 红/左、取消 灰/右）由控件系统渲染，坐标见 syncButtons/init。
     */
    private void renderRepairConfirm(GuiGraphicsExtractor g, Font font, int cx) {
        ModStyle.drawWarning(g, cx, panelY + 60, 30f);
        drawBigCentered(g, font, "修复客户端", cx, panelY + 90, ModStyle.RED, 1.2f);
        drawCentered(g, font, "警告！将会严格将客户端资源与云端对齐，", cx, panelY + 118, ModStyle.TEXT);
        drawCentered(g, font, "您自行添加的内容会被删除！", cx, panelY + 130, ModStyle.RED);
        drawCentered(g, font, "（游戏存档不被影响）", cx, panelY + 142, ModStyle.TEXT_FAINT);
    }

    /**
     * 更新日志滚动区域（纯文本卡片）：标题版本号下方、操作按钮上方，鼠标滚轮上下浏览。
     * 文本用 scissor 裁切在卡片内，右侧细滚动条提示可滚动。
     */
    private void renderChangelog(GuiGraphicsExtractor g, Font font, String log) {
        int lineStep = font.lineHeight + 2;
        int regionX = panelX + 14;
        int regionY = panelY + 62;
        int regionW = PANEL_W - 28;
        int regionH = 126;

        // 卡片容器：描边 + 卡片底
        ModStyle.fillRoundedRect(g, regionX, regionY, regionW, regionH, 5, ModStyle.PANEL_BORDER);
        ModStyle.fillRoundedRect(g, regionX + 1, regionY + 1, regionW - 2, regionH - 2, 4, ModStyle.SURFACE_HIGH);

        // 纯文本呈现（无任何装饰元素），文字区内缩
        int tx = regionX + 9;
        int ty = regionY + 7;
        int tw = regionW - 18;
        int th = regionH - 14;
        List<FormattedCharSequence> lines = font.split(Component.literal(log), tw - 5);
        int contentH = Math.max(lineStep, lines.size() * lineStep - 2);
        double maxScroll = Math.max(0, contentH - th);
        changelogScroll = Math.max(0, Math.min(changelogScroll, maxScroll));

        g.enableScissor(tx, ty, tx + tw, ty + th);
        int y = ty - (int) Math.round(changelogScroll);
        for (FormattedCharSequence line : lines) {
            g.text(font, line, tx, y, ModStyle.TEXT_DIM);
            y += lineStep;
        }
        g.disableScissor();

        // 内容超出可视区域时绘制细滚动条
        if (maxScroll > 0) {
            int barX = regionX + regionW - 6;
            int barH = Math.max(14, (int) (th * (double) th / contentH));
            int barY = ty + (int) Math.round((th - barH) * (changelogScroll / maxScroll));
            ModStyle.fillRoundedRect(g, barX, barY, 2, barH, 1, 0xFF64748B);
        }
    }

    /** 「有新版本」阶段：鼠标滚轮在窗口内滚动更新日志；确认页不响应滚轮。 */
    @Override
    public boolean mouseScrolled(double mouseX, double mouseY, double scrollX, double scrollY) {
        if (repairConfirming) {
            return super.mouseScrolled(mouseX, mouseY, scrollX, scrollY);
        }
        if (engine.phase == Phase.UPDATE_AVAILABLE && scrollY != 0) {
            changelogScroll = Math.max(0, changelogScroll - scrollY * (this.font.lineHeight + 2) * 3);
            return true;
        }
        return super.mouseScrolled(mouseX, mouseY, scrollX, scrollY);
    }

    /**
     * ESC：修复确认页 → 取消确认返回原页；下载中 → 禁止关闭（防打断更新）；其余照常关闭窗口。
     */
    @Override
    public boolean keyPressed(KeyEvent event) {
        if (event.key() == 256) {   // GLFW_KEY_ESCAPE
            if (repairConfirming) {
                closeRepairConfirm();
                return true;
            }
            if (engine.phase == Phase.DOWNLOADING) {
                return true;
            }
        }
        return super.keyPressed(event);
    }

    private void drawCentered(GuiGraphicsExtractor g, Font font, String text, int cx, int y, int color) {
        g.centeredText(font, text, cx, y, color);
    }

    /** 按像素宽度截断文字，超出部分用 … 代替（每字符逐一测量，中文/英文都安全）。 */
    private static String trimToWidth(Font font, String text, int maxWidth) {
        if (text == null || text.isEmpty() || font.width(text) <= maxWidth) {
            return text == null ? "" : text;
        }
        String suffix = "…";
        int budget = maxWidth - font.width(suffix);
        StringBuilder sb = new StringBuilder();
        int used = 0;
        for (int i = 0; i < text.length(); i++) {
            String ch = String.valueOf(text.charAt(i));
            int w = font.width(ch);
            if (used + w > budget) {
                break;
            }
            sb.append(ch);
            used += w;
        }
        return sb + suffix;
    }

    /** 字节数自适应格式：≥1MB 显示一位小数 MB，否则整数 KB。 */
    private static String fmtSize(long bytes) {
        if (bytes >= 1048576L) {
            return String.format(Locale.ROOT, "%.1f MB", bytes / 1048576.0);
        }
        return (bytes / 1024) + " KB";
    }

    /** 下载速度格式：一位小数 MB/s。 */
    private static String fmtSpeed(double bps) {
        return String.format(Locale.ROOT, "%.1f MB/s", bps / 1048576.0);
    }

    /** 放大字号居中绘制（缩放围绕指定中心点）。 */
    private void drawBigCentered(GuiGraphicsExtractor g, Font font, String text, int cx, int y, int color, float scale) {
        var pose = g.pose();
        pose.pushMatrix();
        pose.translate(cx, y);
        pose.scale(scale, scale);
        g.centeredText(font, text, 0, 0, color);
        pose.popMatrix();
    }
}
