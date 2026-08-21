# Gemini 主模型与辅助代理架构设计文档 (2026-08)

## 1. 架构总览

系统采用双通道隔离架构：
1. **主模型直连链路 (Primary / Production)**：服务于 Hermes Agent 核心智能推理、高并发与大上下文任务。
2. **辅助代理链路 (Secondary / VPN)**：服务于移动端 VPN 翻墙、直连 API Key 调用的反向代理。

---

## 2. 链路一：主模型原生直连链路 (agycli2api + 3404 Bridge)

### 2.1 架构图
```
Hermes Agent (OpenAI Client)
      │ (POST http://localhost:3404/chat/completions)
      ▼
Hermes Gemini Bridge (:3404) [Python / ThreadingHTTPServer]
      │ - OpenAI ↔ Gemini 格式双向翻译
      │ - 流式 SSE (Stream) 协议转换
      │ - Function Calling / Tools 结构映射
      │ - 动态模型解析 (gemini-3.7-flash -> gemini-3.7-flash-medium)
      ▼
agycli2api (:3403) [Node.js Express]
      │ - OAuth 凭据自动管理 (~/.gemini/antigravity-cli/antigravity-oauth-token)
      │ - Token 过期前自动保活刷新 (g1-pro-tier / Google AI Pro 专属通道)
      │ - 注入 Session 追踪元数据与会话状态
      ▼
Google Cloud Code 内部网关 (HTTPS 直连)
(https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent)
```

### 2.2 核心特性
- **订阅绑定**：绑定 Google AI Pro (`g1-pro-tier`)，独享超大配额与极速响应。
- **免中间人与 CDN 依赖**：流量在本地 VPS 转换后直通 Google 核心服务，不经过 Cloudflare Workers。
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

### 3.2 环境变量解耦
- 支持通过环境变量 `CF_GEMINI_WORKERS_HOST` 动态修改转发的 Workers 域名，无需改动底层代码：
  ```bash
  # 默认值: gemini-proxy.zzhe0309.workers.dev
  export CF_GEMINI_WORKERS_HOST="new-proxy-domain.workers.dev"
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
python3 -c "import requests; r=requests.post('http://127.0.0.1:3404/chat/completions', json={'model':'gemini-3.7-flash','messages':[{'role':'user','content':'ping'}]}); print(r.status_code, r.json()['choices'][0]['message']['content'])"

# 检查 MITM 代理链路
bash /opt/gemini-proxy/health_check.sh
```
