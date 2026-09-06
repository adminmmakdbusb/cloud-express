package com.speedupdate.gui;

import com.mojang.blaze3d.vertex.PoseStack;
import com.mojang.math.Axis;
import net.minecraft.client.gui.GuiGraphics;

/**
 * 云更新 GUI 视觉规范（颜色 tokens）与通用绘制工具。
 * 与旧项目（生存作弊小套餐）共用同一套设计语言：深色半透明面板、圆角、强调色点缀。
 */
public final class ModStyle {

    // ===== 颜色 tokens =====
    public static final int PANEL_BG = 0xF0141C2C;      // 面板底色（晴空深色，半透明）
    public static final int PANEL_BORDER = 0xFF252B3A;  // 面板描边
    public static final int SURFACE_HIGH = 0xFF1B2639;  // 卡片底（更新日志等）
    public static final int DIVIDER = 0x14FFFFFF;       // 8% 白色分隔线
    public static final int TEXT = 0xFFE8EDF5;          // 主文字
    public static final int TEXT_DIM = 0xFFAAB6C8;      // 次级文字
    public static final int TEXT_FAINT = 0xFF8A94A6;    // 提示文字
    public static final int TEXT_DISABLED = 0xFF565E6B; // 禁用文字
    public static final int GREEN = 0xFF3DDC84;         // 成功 / 已是最新
    public static final int ACCENT = 0xFF38BDF8;        // 请求中 / 强调色
    public static final int ORANGE = 0xFFF97316;        // 有新版本 / 立即重启
    public static final int RED = 0xFFFF7B87;           // 失败 / 网络异常
    public static final int BUTTON_BG = 0xFF232A38;     // 常规按钮底
    public static final int BUTTON_HOVER = 0x14FFFFFF;  // 悬停叠加（8% 白）
    public static final int BUTTON_BORDER = 0xFF2C3344; // 常规按钮描边
    public static final int TRACK_BG = 0xFF0F1727;      // 进度条轨道
    public static final int BTN_PRIMARY = 0xFF10B981;   // 主按钮底（立刻更新·绿）
    public static final int BTN_PRIMARY_TEXT = 0xFF05291E;
    public static final int BTN_BLUE = 0xFF0EA5E9;      // 蓝按钮底（重试）
    public static final int BTN_BLUE_TEXT = 0xFF04222F;
    public static final int BTN_ORANGE = 0xFFF59E0B;    // 橙按钮底（立即重启）
    public static final int BTN_ORANGE_TEXT = 0xFF231505;
    public static final int BTN_RED = 0xFFE5484D;       // 危险按钮底（修复客户端·红）
    public static final int BTN_RED_TEXT = 0xFFFFFFFF;
    public static final int OVERLAY = 0x990A0D14;       // 全屏暗色遮罩

    private ModStyle() {
    }

    /**
     * 圆角矩形填充。四角用逐列近似绘制，radius 建议 3~8，越大越圆。
     */
    public static void fillRoundedRect(GuiGraphics g, int x, int y, int w, int h, int radius, int color) {
        if (w <= 0 || h <= 0) {
            return;
        }
        int r = Math.min(radius, Math.min(w, h) / 2);
        g.fill(x + r, y, x + w - r, y + h, color);
        g.fill(x, y + r, x + w, y + h - r, color);
        for (int i = 0; i < r; i++) {
            int s = (int) Math.round(Math.sqrt(r * r - (r - i - 1) * (r - i - 1)));
            // 左侧圆角（第 i 列距左边缘 i）
            g.fill(x + i, y + r - s, x + i + 1, y + r, color);
            g.fill(x + i, y + h - r, x + i + 1, y + h - r + s, color);
            // 右侧圆角（与左侧镜像：第 i 列距右边缘 i）
            g.fill(x + w - 1 - i, y + r - s, x + w - i, y + r, color);
            g.fill(x + w - 1 - i, y + h - r, x + w - i, y + h - r + s, color);
        }
    }

    /**
     * 面板卡片：1px 描边 + 半透明深色底。
     */
    public static void drawPanel(GuiGraphics g, int x, int y, int w, int h, int radius) {
        fillRoundedRect(g, x, y, w, h, radius, PANEL_BORDER);
        fillRoundedRect(g, x + 1, y + 1, w - 2, h - 2, Math.max(1, radius - 1), PANEL_BG);
    }

    /**
     * 标题：左侧 3px 强调条 + 主文字。
     */
    public static void drawTitle(GuiGraphics g, net.minecraft.client.gui.Font font, int x, int y, String title, int accentColor) {
        fillRoundedRect(g, x, y + 1, 3, font.lineHeight + 2, 2, accentColor);
        g.drawString(font, title, x + 9, y, TEXT);
    }

    /**
     * 1px 分隔细线。
     */
    public static void drawHairline(GuiGraphics g, int x1, int x2, int y) {
        g.fill(x1, y, x2, y + 1, DIVIDER);
    }

    /**
     * 水平进度条：圆角轨道 + 圆角填充。
     */
    public static void drawProgressBar(GuiGraphics g, int x, int y, int w, int h, float progress, int fillColor) {
        fillRoundedRect(g, x, y, w, h, h / 2, TRACK_BG);
        int fillW = (int) (w * Math.max(0f, Math.min(1f, progress)));
        if (fillW > 0) {
            fillRoundedRect(g, x, y, Math.max(h, fillW), h, h / 2, fillColor);
        }
    }

    /**
     * 画一条任意角度的细线（完成勾描边动画用）。pose 旋转 + 细矩形模拟线段。
     */
    public static void drawLine(GuiGraphics g, float x1, float y1, float x2, float y2, float width, int color) {
        PoseStack pose = g.pose();
        pose.pushPose();
        pose.translate(x1, y1, 0);
        float dx = x2 - x1;
        float dy = y2 - y1;
        float len = (float) Math.sqrt(dx * dx + dy * dy);
        if (len < 0.5f) {
            pose.popPose();
            return;
        }
        pose.mulPose(Axis.ZP.rotation((float) Math.atan2(dy, dx)));
        int hw = Math.max(1, (int) Math.ceil(width / 2f));
        g.fill(0, -hw, (int) Math.ceil(len), hw, color);
        pose.popPose();
    }

    /**
     * 请求中旋转加载圈：8 个绕圆点随时间旋转，透明度依次递增形成拖尾。
     * tickMs 传 System.currentTimeMillis()，屏幕每帧渲染自然形成动画。
     */
    public static void drawSpinner(GuiGraphics g, int cx, int cy, float radius, long tickMs) {
        float rot = tickMs * 0.006f;
        for (int i = 0; i < 8; i++) {
            float angle = (float) (i * Math.PI / 4) + rot;
            int alpha = 45 + i * 26;
            int color = (ACCENT & 0x00FFFFFF) | (alpha << 24);
            int x = Math.round(cx + (float) Math.cos(angle) * radius);
            int y = Math.round(cy + (float) Math.sin(angle) * radius);
            g.fill(x - 1, y - 1, x + 1, y + 1, color);
        }
    }

    /**
     * 更新完成描边勾动画（与 UI 预览一致）：先圆环后对勾，t 越界自动钳制；
     * 动画结束后传 t=1 即静态完整图形。size 为 viewBox(52) 的目标边长。
     */
    public static void drawAnimatedCheck(GuiGraphics g, float cx, float cy, float size, float ringT, float checkT) {
        float s = size / 52f;
        float r = 24f * s;
        float w = 2.6f * s;
        // 圆环：从顶部(-90°)顺时针描边
        int segs = 40;
        int drawn = Math.min(segs, Math.max(0, (int) Math.floor(clamp01(ringT) * segs)));
        for (int i = 0; i < drawn; i++) {
            double a1 = Math.toRadians(-90 + 360.0 * i / segs);
            double a2 = Math.toRadians(-90 + 360.0 * (i + 1) / segs);
            drawLine(g, cx + (float) Math.cos(a1) * r, cy + (float) Math.sin(a1) * r,
                    cx + (float) Math.cos(a2) * r, cy + (float) Math.sin(a2) * r, w, GREEN);
        }
        // 对勾：M14,27 L22,35 L38,19（viewBox 52，中心为 26,26）。
        // 注意：pts 是 52x52 虚拟画布坐标，套用到 (cx,cy) 前必须先减去中心 (26,26)，
        // 否则对勾会整体偏移到圆环右下方（偏移量 ≈ 一个半径）。
        float[][] pts = {{14, 27}, {22, 35}, {38, 19}};
        float[] segLen = new float[2];
        float totalLen = 0;
        for (int i = 0; i < 2; i++) {
            segLen[i] = (float) Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);
            totalLen += segLen[i];
        }
        float target = clamp01(checkT) * totalLen;
        for (int i = 0; i < 2 && target > 0; i++) {
            float seg = Math.min(segLen[i], target);
            float frac = segLen[i] > 0 ? seg / segLen[i] : 0;
            float x1 = cx + (pts[i][0] - 26f) * s;
            float y1 = cy + (pts[i][1] - 26f) * s;
            float x2 = cx + (pts[i][0] - 26f + (pts[i + 1][0] - pts[i][0]) * frac) * s;
            float y2 = cy + (pts[i][1] - 26f + (pts[i + 1][1] - pts[i][1]) * frac) * s;
            drawLine(g, x1, y1, x2, y2, w, GREEN);
            target -= seg;
        }
    }

    /**
     * 警告图标：红色描边三角形 + 白色感叹号（「修复客户端」确认页用）。
     * 纯原生绘制、无动画；size 为三角形整体高度。
     */
    public static void drawWarning(GuiGraphics g, float cx, float cy, float size) {
        float halfBase = size * 0.62f;
        float topY = cy - size * 0.50f;
        float botY = cy + size * 0.50f;
        float w = Math.max(1.5f, size / 13f);
        // 红色描边三角：顶 → 左下 → 右下 → 顶
        drawLine(g, cx, topY, cx - halfBase, botY, w, RED);
        drawLine(g, cx - halfBase, botY, cx + halfBase, botY, w, RED);
        drawLine(g, cx + halfBase, botY, cx, topY, w, RED);
        // 白色感叹号：短竖线 + 圆点
        drawLine(g, cx, cy - size * 0.14f, cx, cy + size * 0.16f, w, 0xFFFFFFFF);
        int dotR = Math.max(2, Math.round(size * 0.055f));
        int dotY = Math.round(cy + size * 0.28f);
        g.fill(Math.round(cx) - dotR, dotY - dotR, Math.round(cx) + dotR + 1, dotY + dotR + 1, 0xFFFFFFFF);
    }

    private static float clamp01(float t) {
        return Math.max(0f, Math.min(1f, t));
    }
}
