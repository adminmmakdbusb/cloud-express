package com.speedupdate.gui;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.speedupdate.SpeedUpdate;
import com.speedupdate.update.ServerConfig;
import com.speedupdate.update.UpdateNetwork;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.Font;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.components.Renderable;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;

import java.io.IOException;
import java.net.ConnectException;
import java.net.UnknownHostException;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Pattern;

/**
 * 「更新服务器」设置窗口（26.x 提取式渲染世代）。
 *
 * 入口：标题界面「检查更新」按钮 <b>右键</b>（刻意不做可见入口）。
 * 面板 280x132：左侧上下两个等宽输入框，右侧结果区两行居中，底部三个等大按钮。
 *
 * 功能：
 *   * 填写服务器地址与端口，保存后立即写入 config/speedupdate/server.json 并生效
 *     （更新引擎与启动静默检查都是每次使用前重新读取该文件，因此无需重启游戏）；
 *   * 「测试」在独立守护线程发起 GET_VERSION（5 秒超时），
 *     成功显示服务端返回的内容版本，失败按类型提示原因，全程不阻塞渲染线程；
 *   * 地址容错：自动去掉首尾空格、剥离 http(s):// 前缀与路径；
 *     直接粘贴 {@code host:port} 时会自动把端口拆到端口框。
 *
 * 端口输入框只接受 1~65535 的数字，地址为空或端口非法时「保存」按钮置灰。
 */
public class ServerSettingsScreen extends Screen {

    private static final int PANEL_W = 280;
    private static final int PANEL_H = 132;
    /** 输入框宽度（地址与端口等宽）。 */
    private static final int INPUT_W = 100;
    /** 输入框高度：比常规按钮矮 40%（扁框，文字垂直居中）。 */
    private static final int INPUT_H = 12;
    /** 按钮尺寸（三个等大）。 */
    private static final int BTN_W = 70;
    private static final int BTN_H = 20;
    private static final int BTN_GAP = 6;
    /** 端口输入限制：最多 5 位纯数字。 */
    private static final Pattern PORT_PATTERN = Pattern.compile("\\d{0,5}");
    /** 连接测试超时（毫秒）。 */
    private static final int TEST_TIMEOUT_MS = 5000;

    private final Screen parent;

    private int panelX;
    private int panelY;

    private EditBox hostBox;
    private EditBox portBox;
    private ModWidgets.ActionButton testButton;
    private ModWidgets.ActionButton saveButton;
    private ModWidgets.ActionButton cancelButton;

    /** 右侧结果区两行文字与颜色（异步回调经渲染线程更新）。 */
    private String resultLine1 = "";
    private int resultColor1 = ModStyle.TEXT_FAINT;
    private String resultLine2 = "";
    private int resultColor2 = ModStyle.TEXT_FAINT;
    /** 连接测试进行中：禁用测试按钮，避免点出多个线程。 */
    private volatile boolean testing = false;

    public ServerSettingsScreen(Screen parent) {
        super(Component.literal("更新服务器"));
        this.parent = parent;
    }

    @Override
    protected void init() {
        this.panelX = (this.width - PANEL_W) / 2;
        this.panelY = (this.height - PANEL_H) / 2;
        this.clearWidgets();

        Font font = this.font;
        int x0 = panelX + 20;
        ServerConfig cfg = ServerConfig.load();

        // 地址输入框（无原版边框、无点击高亮，仅保留闪烁光标）
        this.hostBox = new EditBox(font, x0, panelY + 50, INPUT_W, INPUT_H, Component.literal("服务器地址"));
        this.hostBox.setBordered(false);
        this.hostBox.setTextColor(ModStyle.TEXT);
        this.hostBox.setMaxLength(128);
        this.hostBox.setValue(cfg.host);

        // 端口输入框：最多 5 位纯数字
        this.portBox = new EditBox(font, x0, panelY + 82, INPUT_W, INPUT_H, Component.literal("端口"));
        this.portBox.setBordered(false);
        this.portBox.setTextColor(ModStyle.TEXT);
        this.portBox.setMaxLength(5);
        this.portBox.setFilter(s -> PORT_PATTERN.matcher(s).matches());
        this.portBox.setValue(String.valueOf(cfg.tcpPort));

        // 三个等大按钮，整行居中
        int totalW = BTN_W * 3 + BTN_GAP * 2;
        int bx = panelX + (PANEL_W - totalW) / 2;
        int by = panelY + 102;
        this.testButton = new ModWidgets.ActionButton(bx, by, BTN_W, BTN_H, "测试",
                ModStyle.BUTTON_BG, ModStyle.TEXT, true, this::startTest, () -> !testing);
        this.saveButton = new ModWidgets.ActionButton(bx + BTN_W + BTN_GAP, by, BTN_W, BTN_H, "保存",
                ModStyle.BTN_PRIMARY, ModStyle.BTN_PRIMARY_TEXT, this::save, this::isInputValid);
        this.cancelButton = new ModWidgets.ActionButton(bx + (BTN_W + BTN_GAP) * 2, by, BTN_W, BTN_H, "取消",
                ModStyle.BUTTON_BG, ModStyle.TEXT_DIM, true, this::onClose, () -> true);

        addRenderableWidget(hostBox);
        addRenderableWidget(portBox);
        addRenderableWidget(testButton);
        addRenderableWidget(saveButton);
        addRenderableWidget(cancelButton);
        setInitialFocus(hostBox);
    }

    // ================= 输入校验与保存 =================

    /** 地址非空且端口在 1~65535 之间才允许保存。 */
    private boolean isInputValid() {
        int port = parsedPort();
        return !hostPart(normalizeHost(hostBox.getValue())).isEmpty() && port >= 1 && port <= 65535;
    }

    private int parsedPort() {
        try {
            return Integer.parseInt(portBox.getValue().trim());
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    /** 清洗地址：去空格、剥离 http(s):// 前缀与后面的路径部分。 */
    private static String normalizeHost(String raw) {
        String h = raw == null ? "" : raw.trim();
        if (h.regionMatches(true, 0, "http://", 0, 7)) {
            h = h.substring(7);
        } else if (h.regionMatches(true, 0, "https://", 0, 8)) {
            h = h.substring(8);
        }
        int slash = h.indexOf('/');
        if (slash >= 0) {
            h = h.substring(0, slash);
        }
        return h.trim();
    }

    /** 取主机名部分（支持玩家直接粘贴 host:port，此时冒号后的端口会被拆走）。 */
    private static String hostPart(String host) {
        int idx = host.lastIndexOf(':');
        if (idx > 0 && host.indexOf(':') == idx) {
            return host.substring(0, idx).trim();
        }
        return host;
    }

    /** 从 host:port 中取端口；没有则返回 fallback。 */
    private static int portPart(String host, int fallback) {
        int idx = host.lastIndexOf(':');
        if (idx > 0 && host.indexOf(':') == idx) {
            try {
                int p = Integer.parseInt(host.substring(idx + 1).trim());
                if (p >= 1 && p <= 65535) {
                    return p;
                }
            } catch (NumberFormatException ignored) {
                // 冒号后不是合法端口：忽略，沿用端口框的值
            }
        }
        return fallback;
    }

    private void save() {
        String raw = normalizeHost(hostBox.getValue());
        String host = hostPart(raw);
        int port = parsedPort();
        if (host.isEmpty()) {
            setResult("无法保存", ModStyle.RED, "地址不能为空", ModStyle.TEXT_FAINT);
            return;
        }
        // 玩家把 host:port 一起填在地址框时，以地址里的端口为准并回填到端口框
        int embedded = portPart(raw, port);
        if (embedded != port) {
            port = embedded;
            portBox.setValue(String.valueOf(port));
        }
        if (port < 1 || port > 65535) {
            setResult("无法保存", ModStyle.RED, "端口必须是 1~65535", ModStyle.TEXT_FAINT);
            return;
        }
        ServerConfig cfg = ServerConfig.load();
        cfg.host = host;
        cfg.tcpPort = port;
        ServerConfig.save(cfg);
        hostBox.setValue(host);
        setResult("已保存并生效", ModStyle.GREEN, host + ":" + port, ModStyle.TEXT_FAINT);
        SpeedUpdate.LOGGER.info("[云更新] 更新服务器地址已保存：{}:{}", host, port);
    }

    // ================= 连接测试（异步，不阻塞渲染） =================

    private void startTest() {
        String host = hostPart(normalizeHost(hostBox.getValue()));
        int port = parsedPort();
        if (host.isEmpty() || port < 1 || port > 65535) {
            setResult("无法测试", ModStyle.RED, "请先填写正确的地址和端口", ModStyle.TEXT_FAINT);
            return;
        }
        testing = true;
        setResult("正在连接…", ModStyle.ACCENT, host + ":" + port, ModStyle.TEXT_FAINT);
        Thread worker = new Thread(() -> {
            String line1;
            int color1;
            String line2;
            try {
                AtomicReference<java.net.Socket> ref = new AtomicReference<>();
                String raw = UpdateNetwork.tcpCommand(host, port, "GET_VERSION", TEST_TIMEOUT_MS, ref);
                String version = parseVersion(raw);
                if (version.isEmpty()) {
                    line1 = "连接成功";
                    color1 = ModStyle.ORANGE;
                    line2 = "返回内容无法识别";
                } else {
                    line1 = "连接成功";
                    color1 = ModStyle.GREEN;
                    line2 = "内容版本 " + version;
                }
            } catch (UnknownHostException e) {
                line1 = "连接失败";
                color1 = ModStyle.RED;
                line2 = "域名无法解析";
            } catch (ConnectException e) {
                line1 = "连接失败";
                color1 = ModStyle.RED;
                line2 = "端口可能没有开放";
            } catch (IOException e) {
                line1 = "连接失败";
                color1 = ModStyle.RED;
                String msg = String.valueOf(e.getMessage());
                if (msg.contains("超时")) {
                    line2 = "连接超时，请检查地址端口";
                } else if (msg.contains("拒绝")) {
                    line2 = "端口可能没有开放";
                } else {
                    line2 = "无法连接服务器";
                }
            } catch (Exception e) {
                line1 = "连接失败";
                color1 = ModStyle.RED;
                line2 = "无法连接服务器";
            }
            final String f1 = line1;
            final int c1 = color1;
            final String f2 = line2;
            // 回到渲染线程再改 UI 字段
            Minecraft.getInstance().execute(() -> {
                testing = false;
                setResult(f1, c1, f2, ModStyle.TEXT_FAINT);
            });
        }, "云更新-连接测试");
        worker.setDaemon(true);
        worker.start();
    }

    /** 解析 GET_VERSION 回复中的 version 字段；解析失败返回空串。 */
    private static String parseVersion(String raw) {
        try {
            JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
            return obj.has("version") ? obj.get("version").getAsString() : "";
        } catch (Exception e) {
            return "";
        }
    }

    private void setResult(String line1, int color1, String line2, int color2) {
        this.resultLine1 = line1;
        this.resultColor1 = color1;
        this.resultLine2 = line2;
        this.resultColor2 = color2;
    }

    // ================= 渲染与关闭 =================

    @Override
    public void extractRenderState(GuiGraphicsExtractor g, int mouseX, int mouseY, float partialTick) {
        // 26.x 由框架统一处理背景模糊，这里不再调用 renderBackground
        g.fill(0, 0, this.width, this.height, ModStyle.OVERLAY);

        ModStyle.drawPanel(g, panelX, panelY, PANEL_W, PANEL_H, 8);
        Font font = this.font;
        int x0 = panelX + 20;

        // 顶部安全提示
        g.text(font, "请只连接可信任的服务器", x0, panelY + 12, ModStyle.ORANGE);
        ModStyle.drawHairline(g, x0, panelX + PANEL_W - 20, panelY + 26);

        g.text(font, "服务器地址", x0, panelY + 38, ModStyle.TEXT_DIM);
        g.text(font, "端口", x0, panelY + 70, ModStyle.TEXT_DIM);

        // 输入框底色（固定色，无悬停/聚焦变化）与空值占位文字
        drawInputBg(g, hostBox);
        drawInputBg(g, portBox);
        if (hostBox.getValue().isEmpty()) {
            drawPlaceholder(g, font, hostBox, "键入域名或IP");
        }
        if (portBox.getValue().isEmpty()) {
            drawPlaceholder(g, font, portBox, "1-65535");
        }

        // 右侧结果区：两行整体在右侧区块内水平+垂直居中（对齐左侧两个输入框的中心）
        int rx = x0 + INPUT_W + 12;
        int rw = panelX + PANEL_W - 20 - rx;
        if (rw > 20) {
            int rcx = rx + rw / 2;
            if (!resultLine1.isEmpty()) {
                g.centeredText(font, trimToWidth(font, resultLine1, rw), rcx, panelY + 60, resultColor1);
            }
            if (!resultLine2.isEmpty()) {
                g.centeredText(font, trimToWidth(font, resultLine2, rw), rcx, panelY + 76, resultColor2);
            }
        }

        for (Renderable renderable : this.renderables) {
            renderable.extractRenderState(g, mouseX, mouseY, partialTick);
        }
    }

    /** 输入框底：固定深色圆角，不做聚焦高亮（点击无视觉变化，只保留光标）。 */
    private void drawInputBg(GuiGraphicsExtractor g, EditBox box) {
        ModStyle.fillRoundedRect(g, box.getX() - 3, box.getY() - 2, box.getWidth() + 6, box.getHeight() + 4, 4,
                ModStyle.SURFACE_HIGH);
    }

    /** 空值占位文字：淡色，位置与原版 EditBox 内文字一致（左内边距 4、垂直居中）。 */
    private void drawPlaceholder(GuiGraphicsExtractor g, Font font, EditBox box, String text) {
        g.text(font, text, box.getX() + 4, box.getY() + (box.getHeight() - 8) / 2, ModStyle.TEXT_DISABLED);
    }

    /** 超宽文字按宽度截断并加省略号（避免长域名/报错串溢出结果区）。 */
    private static String trimToWidth(Font font, String text, int maxWidth) {
        if (font.width(text) <= maxWidth) {
            return text;
        }
        String ellipsis = "…";
        int ellipsisWidth = font.width(ellipsis);
        StringBuilder sb = new StringBuilder();
        int width = 0;
        for (int i = 0; i < text.length(); i++) {
            String c = String.valueOf(text.charAt(i));
            int cw = font.width(c);
            if (width + cw + ellipsisWidth > maxWidth) {
                break;
            }
            sb.append(c);
            width += cw;
        }
        return sb + ellipsis;
    }

    @Override
    public void onClose() {
        Minecraft.getInstance().setScreen(parent);
    }
}
