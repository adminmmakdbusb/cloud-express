package com.speedupdate.gui;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.AbstractWidget;
import net.minecraft.client.gui.narration.NarratedElementType;
import net.minecraft.client.gui.narration.NarrationElementOutput;
import net.minecraft.client.input.MouseButtonEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;

import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

/**
 * 云更新窗口通用控件。
 * 控件状态通过 Supplier 实时读取，无需手动刷新即可与引擎状态保持同步。
 */
public final class ModWidgets {

    private ModWidgets() {
    }

    /**
     * 动作按钮：圆角底 + 居中文字，点击执行动作。
     * enabled 为 false 时灰显且不可点击；可选描边样式；
     * 文字与颜色可传 Supplier 实时读取（如主界面「有新版本·」绿色提示）。
     */
    public static class ActionButton extends AbstractWidget {
        private final Supplier<String> label;
        private final int bgColor;
        private final Supplier<Integer> textColor;
        private final boolean bordered;
        private final Runnable onPress;
        private final BooleanSupplier enabled;

        public ActionButton(int x, int y, int w, int h, String label, int bgColor, int textColor, Runnable onPress) {
            this(x, y, w, h, label, bgColor, textColor, onPress, () -> true);
        }

        public ActionButton(int x, int y, int w, int h, String label, int bgColor, int textColor, Runnable onPress,
                            BooleanSupplier enabled) {
            this(x, y, w, h, label, bgColor, textColor, false, onPress, enabled);
        }

        public ActionButton(int x, int y, int w, int h, String label, int bgColor, int textColor, boolean bordered,
                            Runnable onPress, BooleanSupplier enabled) {
            this(x, y, w, h, () -> label, bgColor, () -> textColor, bordered, onPress, enabled);
        }

        public ActionButton(int x, int y, int w, int h, Supplier<String> label, int bgColor, Supplier<Integer> textColor,
                            boolean bordered, Runnable onPress, BooleanSupplier enabled) {
            super(x, y, w, h, Component.literal(label.get()));
            this.label = label;
            this.bgColor = bgColor;
            this.textColor = textColor;
            this.bordered = bordered;
            this.onPress = onPress;
            this.enabled = enabled;
        }

        @Override
        protected void renderWidget(GuiGraphics g, int mouseX, int mouseY, float partialTick) {
            Minecraft mc = Minecraft.getInstance();
            boolean en = enabled.getAsBoolean();
            this.active = en;

            int x = getX(), y = getY(), w = getWidth(), h = getHeight();
            if (bordered) {
                ModStyle.fillRoundedRect(g, x, y, w, h, h / 2, en ? ModStyle.BUTTON_BORDER : 0xFF1C222E);
                ModStyle.fillRoundedRect(g, x + 1, y + 1, w - 2, h - 2, Math.max(1, h / 2 - 1), en ? bgColor : 0xFF232A38);
            } else {
                ModStyle.fillRoundedRect(g, x, y, w, h, h / 2, en ? bgColor : 0xFF232A38);
            }
            if (isHoveredOrFocused() && en) {
                ModStyle.fillRoundedRect(g, x, y, w, h, h / 2, ModStyle.BUTTON_HOVER);
            }
            String text = label.get();
            g.drawCenteredString(mc.font, text, x + w / 2, y + (h - mc.font.lineHeight) / 2,
                    en ? textColor.get() : ModStyle.TEXT_DISABLED);
        }

        @Override
        public void onClick(MouseButtonEvent event, boolean isDoubleClick) {
            if (enabled.getAsBoolean()) {
                onPress.run();
            }
        }

        @Override
        protected void updateWidgetNarration(NarrationElementOutput out) {
            out.add(NarratedElementType.TITLE, Component.literal(label.get()));
        }
    }

    /**
     * 图标按钮（如右上角「修复客户端」锤子）：blit 贴图 + 悬停亮显，无文字。
     * texture 为正方形纹理，texSize 为其实际像素边长（256 等）。
     */
    public static class IconButton extends AbstractWidget {
        private final ResourceLocation texture;
        private final int texSize;
        private final Runnable onPress;
        private final BooleanSupplier enabled;

        public IconButton(int x, int y, int size, ResourceLocation texture, int texSize, Runnable onPress) {
            this(x, y, size, texture, texSize, onPress, () -> true);
        }

        public IconButton(int x, int y, int size, ResourceLocation texture, int texSize,
                          Runnable onPress, BooleanSupplier enabled) {
            super(x, y, size, size, Component.literal(""));
            this.texture = texture;
            this.texSize = texSize;
            this.onPress = onPress;
            this.enabled = enabled;
        }

        @Override
        protected void renderWidget(GuiGraphics g, int mouseX, int mouseY, float partialTick) {
            boolean en = enabled.getAsBoolean();
            this.active = en;
            if (en) {
                // 缩放重载贴整图（裁剪重载会只显示左上角透明区）：10 参 blit
                g.blit(texture, getX(), getY(), getWidth(), getHeight(), 0f, 0f, 1f, 1f);
            }
            if (isHoveredOrFocused() && en) {
                ModStyle.fillRoundedRect(g, getX(), getY(), getWidth(), getHeight(), getHeight() / 2, ModStyle.BUTTON_HOVER);
            }
        }

        @Override
        public void onClick(MouseButtonEvent event, boolean isDoubleClick) {
            if (enabled.getAsBoolean()) {
                onPress.run();
            }
        }

        @Override
        protected void updateWidgetNarration(NarrationElementOutput out) {
            out.add(NarratedElementType.TITLE, Component.literal("修复客户端"));
        }
    }
}
