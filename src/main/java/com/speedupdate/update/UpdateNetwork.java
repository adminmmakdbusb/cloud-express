package com.speedupdate.update;

import com.speedupdate.SpeedUpdate;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 底层网络操作（全部为阻塞调用，必须在工作线程执行）：
 *  - tcpCommand：TCP 单行指令（发一行、读一行），完成后立即关闭连接
 *  - tcpFileDownload：TCP 文件下载（GET_FILE 指令：一行元数据 + 原始字节流），完成后立即关闭连接
 *
 * 所有方法都接受 activeSocket 引用：
 * 窗口关闭 / 取消时调用方 close 该 socket，阻塞中的读写立即抛异常退出，
 * 保证服务端不会因客户端挂起而积压连接。
 */
public final class UpdateNetwork {
    private UpdateNetwork() {
    }

    private static final int BUFFER_SIZE = 64 * 1024;

    /** TCP 指令：连接 -> 发一行 -> 读一行 -> 关闭。失败抛 IOException（含超时）。 */
    public static String tcpCommand(String host, int port, String command, int timeoutMillis,
                                    AtomicReference<Socket> activeSocket) throws IOException {
        Socket socket = null;
        try {
            socket = new Socket();
            socket.connect(new InetSocketAddress(host, port), timeoutMillis);
            socket.setSoTimeout(timeoutMillis);
            socket.setTcpNoDelay(true);
            activeSocket.set(socket);

            OutputStream out = socket.getOutputStream();
            out.write((command + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            // 服务端所有指令响应均为一行文本；设置 socket 级读超时兜底
            BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
            String line = reader.readLine();
            if (line == null) {
                throw new IOException("服务端提前关闭了连接");
            }
            return line;
        } catch (SocketTimeoutException e) {
            throw new IOException("连接超时（" + timeoutMillis + "ms）：" + command, e);
        } finally {
            activeSocket.set(null);
            closeQuietly(socket);
        }
    }

    /**
     * TCP 文件下载（GET_FILE|<相对路径>）：
     * 服务端响应单行 "FILE|<字节数>" 后紧接原始文件字节流（无任何分隔符/包装），
     * 客户端按字节数精确读取并写入临时文件，之后由调用方做 SHA-1 校验与移动。
     * listener 每读满一个缓冲块回调一次（received=已下载字节，totalBytes=文件总字节），
     * 供 UI 实时进度使用；可为 null。
     */
    public static void tcpFileDownload(String host, int port, String relPath, Path dest,
                                       int timeoutMillis, AtomicReference<Socket> activeSocket,
                                       AtomicBoolean cancelled, ProgressListener listener) throws IOException {
        Path parent = dest.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }

        Socket socket = null;
        try {
            socket = new Socket();
            socket.connect(new InetSocketAddress(host, port), timeoutMillis);
            socket.setSoTimeout(timeoutMillis);
            socket.setTcpNoDelay(true);
            activeSocket.set(socket);

            OutputStream out = socket.getOutputStream();
            out.write(("GET_FILE|" + relPath + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            // 关键实现细节：必须逐字节读取响应行（不能用 BufferedReader——其内部
            // 预读缓冲会吞掉紧跟在响应行之后的文件字节流，导致下载内容损坏）。
            InputStream in = new BufferedInputStream(socket.getInputStream(), BUFFER_SIZE);
            String statusLine = readLine(in);
            if (statusLine == null) {
                throw new IOException("服务端未返回响应");
            }
            if (statusLine.startsWith("ERROR|")) {
                throw new IOException("服务端拒绝下载（" + statusLine.substring("ERROR|".length()) + "）：" + relPath);
            }
            if (!statusLine.startsWith("FILE|")) {
                throw new IOException("响应格式非法：" + statusLine);
            }
            long size;
            try {
                size = Long.parseLong(statusLine.substring("FILE|".length()).trim());
            } catch (NumberFormatException e) {
                throw new IOException("响应大小非法：" + statusLine);
            }
            if (size < 0) {
                throw new IOException("响应大小非法：" + statusLine);
            }

            // 按声明长度精确读取文件字节流（边读边写，支持取消）
            try (OutputStream fos = new BufferedOutputStream(Files.newOutputStream(dest), BUFFER_SIZE)) {
                long received = 0;
                byte[] buf = new byte[BUFFER_SIZE];
                while (received < size) {
                    if (cancelled.get()) {
                        throw new IOException("下载被取消");
                    }
                    int want = (int) Math.min(buf.length, size - received);
                    int n = in.read(buf, 0, want);
                    if (n < 0) {
                        throw new IOException("连接中断：期望 " + size + " 字节，实际收到 " + received + " 字节");
                    }
                    fos.write(buf, 0, n);
                    received += n;
                    if (listener != null) {
                        listener.onProgress(received, size);
                    }
                }
            }
        } catch (SocketTimeoutException e) {
            throw new IOException("下载超时（" + timeoutMillis + "ms）：" + relPath, e);
        } finally {
            activeSocket.set(null);
            closeQuietly(socket);
        }
    }

    /** 逐字节读取一行（不预读、不缓冲），供二进制流前的元数据行使用。 */
    private static String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder(64);
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\n') {
                break;
            }
            if (c != '\r') {
                sb.append((char) c);
            }
        }
        if (c < 0 && sb.isEmpty()) {
            return null;
        }
        return sb.toString();
    }

    /** 下载进度回调（工作线程）：received=已下载字节，totalBytes=文件总字节。 */
    public interface ProgressListener {
        void onProgress(long received, long totalBytes);
    }

    private static void closeQuietly(Socket socket) {
        if (socket != null) {
            try {
                socket.close();
            } catch (IOException e) {
                SpeedUpdate.LOGGER.debug("[云更新] 关闭 socket 时出错（可忽略）：{}", e.toString());
            }
        }
    }
}
