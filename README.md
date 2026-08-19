# agycli2api

`agycli2api` 是一个轻量级的单用户 API 代理服务，将请求代理至 Google Cloud Code (`daily-cloudcode-pa.googleapis.com`) 基础设施。它暴露标准的 **Gemini API** 接口；OpenAI `/chat/completions` 格式由随附的 Python 桥接模块（`bridge.py`，端口 `3404`）完整支持。

---

## ✨ 核心特性

- **双接口兼容**：
  - **Gemini 原生接口**：支持 `GET /v1beta/models` 查询模型列表及 `POST /v1beta/models/:model:generateContent` 生成内容。
  - **OpenAI 兼容接口**：由 `bridge.py`（端口 `3404`）提供 `POST /chat/completions` 路由，方便支持 OpenAI 格式的客户端（如 Hermes、LangChain、Claude Code 等）无缝接入，完整支持流式、Tool Calling、模型名映射与 `reasoning_effort`。
- **完整 Tool / Function Calling 支持**：`bridge.py` 自动完成 OpenAI Tools 格式与 Gemini `functionDeclarations` 之间的双向映射。
- **流式传输 (SSE)**：全量支持 Server-Sent Events (SSE) 流式响应 Pass-through。
- **思维链 (Thinking Budget) 优化**：自动调优 `maxOutputTokens`，避免由于思考过程消耗 Token 导致输出截断。
- **防封与遥测伪装**：自动重写 `User-Agent`、客户端版本号与 Headers，完全一致化匹配官方 [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)。
- **无缝 OAuth 鉴权**：直接读取并自动刷新 Antigravity CLI 生成与管理的 OAuth 凭据。
- **独立 Python 桥接模块 (`bridge.py`)**：提供独立的 Python Bridge (默认监听 `3404` 端口)，用于特定的框架桥接与灵活定制。

---

## 🚀 快速开始

### 方式 1：Docker 部署（推荐）

可以通过 Docker 快速启动 `agycli2api`，配置文件位于 `docker/docker-compose.yml`：

```bash
docker-compose -f docker/docker-compose.yml up -d
```

#### 前置要求
代理服务依赖于 Antigravity CLI 生成的凭据：
1. 服务会自动读取并刷新 `~/.gemini/antigravity-cli/antigravity-oauth-token` 凭据。
2. 它会从 `~/.gemini/antigravity-cli/conversations/` 中提取会话 ID。

在 Docker 中运行时：
- 可以在容器内执行初始化命令生成凭据：
  ```bash
  docker-compose -f docker/docker-compose.yml exec agycli2api /root/.local/bin/agy -p hi
  ```
- 或者直接将宿主机的 `~/.gemini` 目录挂载进容器。

---

### 方式 2：本地 npm 部署

```bash
# 1. 安装依赖
npm install

# 2. 构建 TypeScript
npm run build

# 3. 启动服务
npm run start
```

开发模式（支持热重载）：
```bash
npm run dev
```

默认服务启动在 `http://localhost:3403`。

---

### 方式 3：Python 桥接服务 (`bridge.py`)

如果你需要独立运行 Python Bridge：

```bash
python3 bridge.py
```
桥接服务默认监听 `3404` 端口，并将请求转换后转发至 `3403` 的主服务。

---

## 📡 请求示例

### 1. 查询可用模型列表

```bash
curl "http://localhost:3403/v1beta/models?key=YOUR_API_KEY"
```

使用内置工具脚本打印可视化模型表格：

```bash
AGYCLI2API_URL=http://localhost:3403 \
AGYCLI2API_KEY=YOUR_API_KEY \
npm run list-models
```

也可直接传递命令行参数：
```bash
npm run list-models -- --url http://localhost:3403 --key YOUR_API_KEY
```

---

### 2. Gemini 原生请求示例

```bash
curl -X POST http://localhost:3403/v1beta/models/gemini-3-flash:generateContent \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: YOUR_API_KEY" \
  -d '{
    "contents": [{
      "parts": [{"text": "你好，请介绍一下你自己。"}],
      "role": "user"
    }]
  }'
```

---

### 3. OpenAI 格式 `/chat/completions` 请求示例

OpenAI 格式由 `bridge.py` 桥接层提供（默认端口 `3404`，需先启动 `python3 bridge.py`）：

```bash
curl -X POST http://localhost:3404/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "gemini-3.6-flash-medium",
    "messages": [
      {"role": "user", "content": "你好！"}
    ],
    "temperature": 0.7
  }'
```

---

## ⚙️ 代理逻辑与遥测模拟

为了精准模拟官方 Antigravity IDE 插件的行为并避免风控：

- **基于内容哈希的会话识别**：由于 API 为无状态 HTTP 请求，代理根据对话历史 (`contents`) 计算哈希，将连续请求关联到统一的逻辑 Session。
- **动态 Step Index 伪装**：模拟真实的 IDE 后台步骤演进，新 Session 从 step `3` 开始，后续请求随机递增 `2`~`5` 个 step。
- **Execution ID 注入**：追踪用户发言轮次，非首轮对话时自动生成随机 UUID 作为 `last_execution_id` 注入到请求标记中。

---

## 🔧 环境变量配置

可通过设置环境变量来自定义代理行为：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `AGYCLI2API_KEY` | 随机生成的 32 位 Hex | 访问代理服务的 API Key。若未配置，启���时会自动生成并在日志中打印 |
| `PORT` | `3403` | 服务监听端口 |
| `ANTIGRAVITY_VERSION` | `1.0.6` | 覆盖用于 `User-Agent` 的 Antigravity CLI 版本号 |
| `ANTIGRAVITY_SESSION_ID` | 自动/随机生成 | 指定请求使用的固定 Session ID |
| `INJECT_SYSTEM_PROMPT` | `false` | 设置为 `true` 以注入默认系统指令 Payload |

---

## 🔑 OAuth Token 维护与失效重新登录指南

代理服务自动读取并自动刷新 `~/.gemini/antigravity-cli/antigravity-oauth-token` 凭据。日常使用无需人工干预。

若遇 Google 密码变更或主动撤销授权导致 Token 彻底失效，可通过以下两种方式重登：

### 方式 1：VPS 本地一键设备码授权（推荐）

在 VPS 终端执行自带的授权脚本：
```bash
python3 /opt/agycli2api/scripts/relogin.py
```
- 脚本会生成形如 `https://www.google.com/device` 的授权网址及 8 位验证码。
- 在浏览器打开并确认授权后，脚本会自动生成合规的 `antigravity-oauth-token` 凭证并写入对应路径。
- 重启服务生效：`systemctl restart agycli2api.service`

### 方式 2：本地电脑复制

在支持指令集的本地 Windows / Mac 电脑上运行官方 CLI 登录：
```bash
agy auth login
```
将本地生成的凭据文件复制覆盖到 VPS 的 `~/.gemini/antigravity-cli/antigravity-oauth-token`：
- **Windows 本地路径**：`%USERPROFILE%\.gemini\antigravity-cli\antigravity-oauth-token`
- **Mac/Linux 本地路径**：`~/.gemini/antigravity-cli/antigravity-oauth-token`

---

## 📄 开源协议

基于 [MIT License](LICENSE) 开源。

---

## 🙏 致谢 / Credits

本项目基于开源项目 [Arocial/agycli2api](https://github.com/Arocial/agycli2api) 进行二次开发与功能扩展。感谢原作者及开源社区的贡献！

