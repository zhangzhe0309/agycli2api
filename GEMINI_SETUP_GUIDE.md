# Gemini 3.7 Flash 本地代理链路与跨机部署指南 (2026-08)

## 1. 架构总览

系统采用纯本地闭环代理架构，结合 Google Antigravity OAuth、Cloudflare Workers 全球边缘网关与多线程协议转换桥，同时支持单机部署与跨 VPS 隔离：

```
[ Hermes Agent (OpenAI 格式) ]
               │ (POST http://localhost:3404/v1/chat/completions)
               ▼
[ hermes-gemini-bridge.service (端口: 3404) ]
               │ - OpenAI ↔ Gemini 原生协议转换
               │ - 流式 SSE (Stream) 协议转换
               │ - Tool / Function Calling 双向映射
               │ - v4.1 内置 4 次退避重试 (2s/5s/9s/15s)
               ▼
[ agycli2api.service (端口: 3403) ]
               │ - Google Antigravity OAuth Token 自动续期
               │ - Google AI Pro (g1-pro-tier) 专属配额通道
               │ - ANTIGRAVITY_ENDPOINT_DAILY 环境变量路由
               ▼
[ Cloudflare Workers 边缘代理 (gemini-cloudcode-proxy) ]
               │ (https://gemini-cloudcode-proxy.zzhe0309.workers.dev)
               │ - 全球 Anycast CDN 边缘出口，彻底规避机房 IP 地理与频率限制
               ▼
[ Google Cloud Code / Gemini API 核心网关 ]
               (https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent)
```

---

## 2. 账号分配与多 VPS 隔离规范

- **104 VPS (本地主服)**：
  - **绑定 Google 账号**：`zzhe0309@gmail.com` (Google AI Pro 订阅)
  - **Token 路径**：`/root/.gemini/antigravity-cli/antigravity-oauth-token`
  - **上游网关**：`https://gemini-cloudcode-proxy.zzhe0309.workers.dev`
- **192 VPS (远端独立服)**：
  - **绑定 Google 账号**：`qifan007@gmail.com` (Google AI Pro 订阅)
  - **Token 路径**：`/root/.gemini/antigravity-cli/antigravity-oauth-token`
  - **隔离铁律**：两台 VPS 各自独立运行本地代理与专属账号，凭据互为独立备份，严禁跨机混用覆盖。

---

## 3. Cloudflare Workers 边缘网关部署步骤

为了彻底解决 VPS 数据中心 IP 访问 Google 偶发报 `User location is not supported for the API use.` (FAILED_PRECONDITION 400)，通过 Cloudflare Workers 构建轻量透明的反向代理。

### 3.1 授权 Wrangler
在 VPS 终端执行设备码授权：
```bash
npx --yes wrangler login --device
```
打开输出的验证 URL（`https://dash.cloudflare.com/oauth2/device/verify?user_code=xxxx`），登录 Cloudflare 账号并点击 **Allow** 授权。

### 3.2 创建 Worker 项目
```bash
mkdir -p /root/cloudflare-gemini-worker/src
cd /root/cloudflare-gemini-worker

cat << 'EOF' > wrangler.json
{
  "name": "gemini-cloudcode-proxy",
  "main": "src/index.js",
  "compatibility_date": "2026-08-25"
}
EOF

cat << 'EOF' > src/index.js
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const targetHost = "daily-cloudcode-pa.googleapis.com";
    url.hostname = targetHost;
    url.protocol = "https:";
    url.port = "443";

    const newHeaders = new Headers(request.headers);
    newHeaders.set("Host", targetHost);

    const init = {
      method: request.method,
      headers: newHeaders,
      body: request.body,
      redirect: "follow"
    };

    return fetch(url.toString(), init);
  }
};
EOF
```

### 3.3 发布部署
```bash
npx wrangler deploy
```
部署完成后记录返回的域名（如 `https://gemini-cloudcode-proxy.zzhe0309.workers.dev`）。

---

## 4. 核心组件与 Systemd 服务配置

### 4.1 agycli2api.service (端口: 3403)
- **目录**：`/opt/agycli2api`
- **代码特性**：`src/config.ts` 支持 `process.env.ANTIGRAVITY_ENDPOINT_DAILY`
- **配置文件**：`/etc/systemd/system/agycli2api.service`
```ini
[Unit]
Description=Antigravity CLI2API Proxy (Gemini API via agy OAuth)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/agycli2api
Environment=PORT=3403
Environment=AGYCLI2API_KEY=hermes-agy-proxy-2026
Environment=ANTIGRAVITY_ENDPOINT_DAILY=https://gemini-cloudcode-proxy.zzhe0309.workers.dev
Environment=NODE_OPTIONS=--max-old-space-size=64
Environment=HOME=/root
ExecStart=/usr/bin/node --use-env-proxy --use-system-ca dist/index.js
Restart=always
RestartSec=5
MemoryMax=96M

[Install]
WantedBy=multi-user.target
```

### 4.2 hermes-gemini-bridge.service (端口: 3404)
- **目录**：`/opt/agycli2api`
- **入口命令**：`/usr/bin/python3 bridge.py 3404`
- **特性**：
  - 双向兼容 OpenAI `/v1/chat/completions` 与 `/chat/completions`。
  - 支持 Tool/Function Calling 结构映射。
  - 支持 SSE 流式传输（Streaming）与 Gemini 3.x Thought Signature。
  - 内置 v4.1 指数退避重试（针对 400 Soft-block 自动切换上游前端重试）。
- **配置文件**：`/etc/systemd/system/hermes-gemini-bridge.service`
```ini
[Unit]
Description=Gemini OpenAI↔Native Bridge (3403→3404)
After=agycli2api.service
Requires=agycli2api.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/agycli2api
ExecStart=/usr/bin/python3 bridge.py 3404
Restart=always
RestartSec=3
MemoryMax=64M

[Install]
WantedBy=multi-user.target
```

---

## 5. Hermes 配置文件规范

### 5.1 Hermes 配置文件 (`~/.hermes/config.yaml`)
```yaml
model:
  default: gemini-3.7-flash
  provider: gemini
  base_url: http://localhost:3404

providers:
  gemini:
    base_url: http://localhost:3404
    key_env: GEMINI_API_KEY
  zai:
    base_url: https://open.bigmodel.cn/api/coding/paas/v4
    key_env: ZAI_API_KEY
  agnes2:
    base_url: https://apihub.agnes-ai.com/v1
    key_env: AGNES_API_KEY_2
  sensenova:
    base_url: https://token.sensenova.cn/v1
    key_env: SENSENOVA_API_KEY
  nvidia:
    base_url: https://integrate.api.nvidia.com/v1
    key_env: NVIDIA_API_KEY

fallback_providers:
  - provider: zai
    model: glm-5.2
    base_url: https://open.bigmodel.cn/api/coding/paas/v4
  - provider: agnes2
    model: agnes-2.5-flash
    base_url: https://apihub.agnes-ai.com/v1
  - provider: sensenova
    model: sensenova-6.7-flash-lite
    base_url: https://token.sensenova.cn/v1
  - provider: nvidia
    model: deepseek-ai/deepseek-v4-flash-0731
    base_url: https://integrate.api.nvidia.com/v1
```

### 5.2 环境变量文件 (`~/.hermes/.env`)
```env
GEMINI_API_KEY=hermes-agy-proxy-2026
ZAI_API_KEY=b83ae4a99a7a4925a97058b7180cea89.g96vPfru8d7eqzbR
```

---

## 6. 运维与验证命令

### 6.1 服务重载与重启
```bash
systemctl daemon-reload
systemctl restart agycli2api.service hermes-gemini-bridge.service
```

### 6.2 连通性测试 (OpenAI 协议)
```bash
curl -s http://127.0.0.1:3404/v1/chat/completions \
  -H "Authorization: Bearer hermes-agy-proxy-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 10
  }'
```

### 6.3 流式响应测试 (SSE)
```bash
curl -N http://127.0.0.1:3404/v1/chat/completions \
  -H "Authorization: Bearer hermes-agy-proxy-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": true,
    "max_tokens": 15
  }'
```
