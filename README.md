# Cloud Express · 云端速递

![cover](docs/cover.png)

整合包云更新系统 —— 为 NeoForge 1.21.1 魔改整合包提供**游戏内一键增量更新**（模组 / 光影包 / 资源包），
配套自建 TCP 服务端，由整合包作者维护分发内容。

## 特性

- 游戏主界面「检查更新」：启动静默比对版本，发现新版按钮变绿
- SHA-1 内容比对，只下载变化的文件；下载后校验完整再安装，失败自动重试
- 支持三类内容：`mods/*.jar`、`shaderpacks/*.zip`、`resourcepacks/*.zip`
- **只管理带 `_ZAKO` 标识的云端文件**——玩家自己添加的模组 / 光影 / 资源包不会被更新删除
- 「修复客户端」：需要与云端严格对齐时一键清理本地多余文件（会删除玩家自装内容，操作前有警告）
- 旧文件自动清理（静默预删 + 重启删除闭环）
- 首次运行自动释放配套服务端程序到 `config/speedupdate/server.py`

## 仓库结构

```
src/     客户端模组源码（NeoForge 1.21.1，Java 21）
serve.py 服务端程序（Python 3，双击可运行；与 jar 内置模板同源）
docs/    封面等素材
```

## 服务端部署（整合包作者）

1. 获取 `serve.py`（仓库根 / Releases 附件 / 或客户端首次运行后释放到 `config/speedupdate/server.py`），双击运行（需 Python 3）
2. 首次启动自动生成 `speedupdate.conf` 与 `mods/`、`shaderpacks/`、`resourcepacks/` 目录
3. 配置 `speedupdate.conf`：`port`（默认 10086）、`version`（内容版本）、`feedback_url` 等
4. 把要分发的文件放入对应目录（文件名带版本尾缀 `_v<版本>_ZAKO`，服务端每次启动自动补齐）
5. 玩家侧在 `config/speedupdate/server.json` 填你的服务器地址

## 客户端配置

首次启动自动生成 `config/speedupdate/server.json`：

```json
{
  "host": "127.0.0.1",
  "tcpPort": 10086
}
```

> 本模组会连接你配置的更新服务器并按其指令同步文件。**请只连接你信任的整合包作者的服务器。**

## 构建

需要 JDK 17~21（推荐 21）：

```bash
gradlew build
```

产物：`build/libs/speedupdate-2.0.0.jar`（放入 `.minecraft/mods/`）。

## 许可

[MIT](LICENSE)
