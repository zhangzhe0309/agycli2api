# Gemini 3.7 Flash 本地代理链路与跨机部署指南 (2026-08)

## 1. 架构总览

系统采用纯本地闭环代理架构，结合 Google Antigravity OAuth 与多线程协议转换桥，���时支持单机部署与跨 VPS 共享（104 主机 -> 192 主机持久反向隧道）：

```
[ 104 本机 Hermes Agent ] ──┐
                            │ (OpenAI 协议: /v1/chat/completions)
[ 192 远端 Hermes Agent ] ──┼──> [ gemini-tunnel-to-192.service (SSH 反向隧道) ]
                            │
                            ▼
           [ hermes-gemini-bridge.service (端口: 3404) ]
                            │ (Gemini 原生协议 / SSE 流式 / Tool Calling 映射)
                            ▼
           [ agycli2api.service (端口: 3403) ]
                            │ (Google Antigravity OAuth Token / Google AI Pro 额度)
                            ▼
           [ Google Cloud Code / Gemini API 核心网关 ]
```

---

## 2. 核心组件与 Systemd 服务清单

### 2.1 agycli2api.service (端口: 3403)
- **定位**：OAuth 鉴权与 Google 内网网关通信代理。
- **目录**：`/opt/agycli2api`
- **入口命令**：`/usr/bin/node --use-env-proxy --use-system-ca dist/index.js`
- **Token 路径**：`/root/.gemini/antigravity-cli/antigravity-oauth-token`
- **特性**：
  - 自动管理与刷新 OAuth Access Token（绑定 Google AI Pro `g1-pro-tier` 额度）。
  - 内置内存上限保护（`MemoryMax=96M`）。

### 2.2 hermes-gemini-bridge.service (端口: 3404)
- **定位**：OpenAI ↔ Gemini 原生协议转换桥（Python ThreadingHTTPServer）。
- **工作目录**：`/opt/agycli2api`
- **入口命令**：`/usr/bin/python3 bridge.py 3404`
- **特性**：
  - 双向兼容 OpenAI `/v1/chat/completions` 与 `/chat/completions`。
  - 支持 Tool/Function Calling 结构无损映射。
  - 支持 SSE 流式传输（Streaming）与 Gemini 3.x Thought Signature。
  - 支持请求鉴权（校验 Bearer Token: `hermes-agy-proxy-2026`）。
  - 内置内存上限保护（`MemoryMax=64M`）。

### 2.3 gemini-tunnel-to-192.service (跨主机反向隧道)
- **定位**：将 104 机本地 3404 端口安全穿透到 192.3.248.147 的 `127.0.0.1:3404`。
- **入口命令**：`/usr/bin/ssh -N -o StrictHostKeyChecking=no -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -R 3404:127.0.0.1:3404 root@192.3.248.147`
- **效果**：192 机上的 Hermes Agent 无需重复配置 OAuth，可直接走本地 `http://localhost:3404` 共享 Pro 额度并避开机房 IP 限制。

### 2.4 gemini-proxy.service (端口: 18080，辅助 MITM 代理)
- **定位**：为移动端 VPN / sing-box 分流提供的 Gemini TLS 证书动态签发与反代服务。
- **目录**：`/opt/gemini-proxy`

---

## 3. 配置文件规范

### 3.1 Hermes 配置文件 (`~/.hermes/config.yaml`)

```yaml
model:
  default: gemini-3.7-flash
  provider: gemini
  base_url: http://localhost:3404

providers:
  gemini:
    base_url: http://localhost:3404
    key_env: GEMINI_API_KEY

fallback_providers:
  - provider: agnes2
    model: agnes-2.5-flash
    base_url: https://apihub.agnes-ai.com/v1
  - provider: sensenova
    model: sensenova-6.7-flash-lite
    base_url: https://token.sensenova.cn/v1
  - provider: nvidia
    model: deepseek-ai/deepseek-v4-flash-0731
    base_url: https://integrate.api.nvidia.com/v1
  - provider: zai
    model: glm-5.2
    base_url: https://open.bigmodel.cn/api/paas/v4
```

### 3.2 环境变量文件 (`~/.hermes/.env`)

```env
GEMINI_API_KEY=hermes-agy-proxy-2026
```

> **注意**：`hermes-agy-proxy-2026` 为 3404 Bridge 内部通信安全 Token，防止未授权请求。

---

## 4. 常用服务运维命令

### 4.1 查看服务状态与日志
```bash
# 查看所有 Gemini 相关服务状态
systemctl status agycli2api.service hermes-gemini-bridge.service gemini-tunnel-to-192.service --no-pager

# 查看 Bridge 实时日志
journalctl -u hermes-gemini-bridge.service -f -n 50

# 查看 OAuth 代理日志（查看 Token 刷新情况）
journalctl -u agycli2api.service -f -n 50
```

### 4.2 重启与重载
```bash
systemctl daemon-reload
systemctl restart agycli2api.service hermes-gemini-bridge.service gemini-tunnel-to-192.service
```

---

## 5. 连通性测试与验证流程

### 5.1 基础接口测试 (Curl)
```bash
curl -s http://127.0.0.1:3404/v1/chat/completions \
  -H "Authorization: Bearer hermes-agy-proxy-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "ping"}]
  }'
```

**预期返回**：包含 `"content": "..."` 的标准 OpenAI 响应 JSON。

### 5.2 跨机 192 节点连通性测试
在 192.3.248.147 主机上执行相同 curl 命令，验证 `127.0.0.1:3404` 穿透是否正常。

---

## 6. 常见问题排查 (Troubleshooting)

1. **报错 `Unauthorized: Invalid API Key`**
   - 检查请求头是否携带 `Authorization: Bearer hermes-agy-proxy-2026`。
   - 检查 `~/.hermes/.env` 中 `GEMINI_API_KEY` 是否正确配置。

2. **Token 过期或报 401/403**
   - `agycli2api` 内置每小时自动刷新机制。如果手动调试，可检查 `/root/.gemini/antigravity-cli/antigravity-oauth-token` 文件修改时间或重启 `agycli2api.service`。

3. **192 主机无法连接 3404 端口**
   - 在 104 机上检查 `systemctl status gemini-tunnel-to-192.service`，确保 SSH 反向隧道进程正常存活。
