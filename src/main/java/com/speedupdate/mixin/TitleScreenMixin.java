package com.speedupdate.mixin;

import com.speedupdate.gui.ModStyle;
import com.speedupdate.gui.ModWidgets;
import com.speedupdate.gui.UpdateScreen;
import com.speedupdate.update.UpdateStatus;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.AbstractWidget;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.network.chat.Component;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * 主界面「检查更新」按钮：
 * 在「退出游戏」按钮正下方添加一个同尺寸的按钮，点击打开更新窗口。
 *
 * 注意：NeoForge 运行时的标题界面布局与原版不同（退出按钮为 98x20，
 * 位于选项按钮右侧），因此这里动态查找退出按钮并对齐其坐标与尺寸，
 * 兼容原版与 NeoForge 布局，也兼容其他 mod 对标题界面的调整。
 */
@Mixin(TitleScreen.class)
public abstract class TitleScreenMixin extends Screen {

    protected TitleScreenMixin(Component pTitle) {
        super(pTitle);
    }

    @Inject(method = "init", at = @At("TAIL"))
    private void speedupdate$addCheckUpdateButton(CallbackInfo ci) {
        int x = -1;
        int y = 0;
        int w = 200;
        int h = 20;

        // 找到「退出游戏」按钮：同语言环境下对比按钮文字，避免硬编码坐标
        String quitText = Component.translatable("menu.quit").getString();
        for (var child : this.children()) {
            if (child instanceof AbstractWidget widget && widget.getMessage() != null
                    && widget.getMessage().getString().equals(quitText)) {
                x = widget.getX();
                y = widget.getY() + widget.getHeight() + 4;
                w = widget.getWidth();
                h = widget.getHeight();
                break;
            }
        }

        // 兜底：找不到退出按钮时使用原版默认位置
        if (x < 0) {
            x = this.width / 2 - 100;
            y = this.height / 4 + 48 + 84 + 24;
            w = 200;
            h = 20;
        }

        int bx = x;
        int by = y;
        int bw = w;
        int bh = h;
        // 启动静默检查发现新版本时：绿色「有新版本·」；更新完成后自动恢复默认样式
        this.addRenderableWidget(new ModWidgets.ActionButton(bx, by, bw, bh,
                () -> UpdateStatus.newVersionAvailable ? "有新版本·" : "检查更新",
                ModStyle.BUTTON_BG,
                () -> UpdateStatus.newVersionAvailable ? ModStyle.GREEN : ModStyle.TEXT,
                true,
                () -> Minecraft.getInstance().setScreenAndShow(new UpdateScreen()), () -> true));
    }
}
