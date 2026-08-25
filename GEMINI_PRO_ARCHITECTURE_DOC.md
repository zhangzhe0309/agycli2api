# Gemini 主模型与辅助代理架构设计文档 (2026-08)

## 1. 架构总览

系统采用双通道隔离架构：
1. **主模型直连与边缘网关链路 (Primary / Production)**：服务于 Hermes Agent 核心智能推理、高并发与大上下文任务，集成 Cloudflare Workers 全球边缘网关与 Google AI Pro 专属配额。
2. **辅助代理链路 (Secondary / VPN)**：服务于移动端 VPN 翻墙、直连 API Key 调用的反向代理。

---

## 2. 链路一：主模型原生直连链路 (agycli2api + 3404 Bridge + CF Worker)

### 2.1 架构图
```
Hermes Agent (OpenAI Client)
      │ (POST http://localhost:3404/v1/chat/completions)
      ▼
Hermes Gemini Bridge (:3404) [Python / ThreadingHTTPServer]
      │ - OpenAI ↔ Gemini 格式双向翻译
      │ - 流式 SSE (Stream) 协议转换
      │ - Function Calling / Tools 结构映射
      │ - 动态模型解析 (gemini-3.7-flash -> gemini-3.7-flash-medium)
      │ - v4.1 指数退避重试 (Location soft-block 自动重试)
      ▼
agycli2api (:3403) [Node.js Express]
      │ - OAuth 凭据自动管理 (~/.gemini/antigravity-cli/antigravity-oauth-token)
      │ - Token 过期前自动保活刷新 (g1-pro-tier / Google AI Pro 专属通道)
      │ - ANTIGRAVITY_ENDPOINT_DAILY 动态路由
      ▼
Cloudflare Workers 全球边缘网关 (gemini-cloudcode-proxy)
      │ (https://gemini-cloudcode-proxy.zzhe0309.workers.dev)
      │ - Anycast CDN 出口，彻底消除机房 IP 地理/并发软封锁
      ▼
Google Cloud Code 内部网关 (HTTPS)
(https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent)
```

### 2.2 核心特性
- **订阅绑定**：绑定 Google AI Pro (`g1-pro-tier`)，独享超大配额与极速响应。
- **边缘网关保护**：通过 Cloudflare Workers 将流量打散至全球边缘，防止数据中心 IP 触发 Google 的 `User location is not supported` 限制。
- **高上下文支持**：支持 1,048,576 Token 输入上下文与 65,536 Token 最大输出。

---

## 3. 链路二：辅助 MITM 代理链路 (gemini-proxy :18080)

### 3.1 架构图
```
VPN 客户端 (Android v2rayNG)
      ▼
VLESS-WS (:8080) / VLESS-REALITY (:443) [sing-box]
      │ (分流规则：命中 generativelanguage.googleapis.com)
      ▼
gemini-proxy (:18080) [Python MITM 代理]
      │ - TLS 终结与动态证书签发 (本地 CA: /opt/gemini-proxy/ca.pem)
      │ - 目标重定向 (可配置 CF_GEMINI_WORKERS_HOST)
      ▼
Cloudflare Workers 反代 (https://gemini-proxy.zzhe0309.workers.dev)
      ▼
Google Gemini 公共 API (https://generativelanguage.googleapis.com)
```

---

## 4. 服务运维与监控规范

### 4.1 Systemd 守护进程
- `hermes-gemini-bridge.service` (端口 3404, 内存限制 64M)
- `agycli2api.service` (端口 3403, 内存限制 96M)
- `gemini-proxy.service` (端口 18080, 内存限制 64M)
- `sing-box.service` (端口 443, 8080, 内存限制 256M)

### 4.2 一键健康检查
```bash
# 检查主模型链路 (3404)
python3 -c "import urllib.request, json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:3404/v1/chat/completions', data=json.dumps({'model':'gemini-3.7-flash','messages':[{'role':'user','content':'ping'}]}).encode(), headers={'Authorization':'Bearer hermes-agy-proxy-2026','Content-Type':'application/json'})); print('Status:', r.status, json.loads(r.read())['choices'][0]['message']['content'])"

# 检查 MITM 代理链路
bash /opt/gemini-proxy/health_check.sh
```
