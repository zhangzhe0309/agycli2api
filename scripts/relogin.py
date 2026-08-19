#!/usr/bin/env python3
"""Google OAuth Device Code login helper for agycli2api on headless VPS.

Run this script to obtain a new OAuth token when the current token is revoked or expired:
    python3 /opt/agycli2api/scripts/relogin.py
"""

import json
import os
import sys
import time
import requests

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email"

TOKEN_DIR = os.path.expanduser("~/.gemini/antigravity-cli")
TOKEN_PATH = os.path.join(TOKEN_DIR, "antigravity-oauth-token")


def main():
    print("=== Antigravity Google OAuth Device Login ===")
    print("1. Requesting Device Authorization Code from Google...")

    resp = requests.post(
        DEVICE_CODE_URL,
        data={
            "client_id": CLIENT_ID,
            "scope": SCOPES,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"Failed to request device code: HTTP {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_url = data.get("verification_url", "https://www.google.com/device")
    expires_in = data.get("expires_in", 1800)
    interval = data.get("interval", 5)

    print("\n------------------------------------------------------------")
    print(f"请在浏览器中打开: {verification_url}")
    print(f"并输入验证码:     {user_code}")
    print("------------------------------------------------------------\n")
    print("等待授权中 (按 Ctrl+C 可取消)...")

    start_time = time.time()
    while time.time() - start_time < expires_in:
        time.sleep(interval)
        token_resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
        )

        if token_resp.status_code == 200:
            token_data = token_resp.json()
            print("\n[成功] 授权成功！正在保存凭证...")

            os.makedirs(TOKEN_DIR, exist_ok=True)

            # Build token JSON compatible with Antigravity CLI / agycli2api
            now = time.time()
            exp_in = token_data.get("expires_in", 3600)
            exp_date = int((now + exp_in) * 1000)

            payload = {
                "auth_method": "consumer",
                "token": {
                    "access_token": token_data["access_token"],
                    "refresh_token": token_data.get("refresh_token", ""),
                    "expires_in": exp_in,
                    "expiry_date": exp_date,
                    "token_type": token_data.get("token_type", "Bearer"),
                    "scope": token_data.get("scope", SCOPES),
                },
            }

            # Backup old token if present
            if os.path.exists(TOKEN_PATH):
                backup_path = f"{TOKEN_PATH}.bak.{int(now)}"
                try:
                    with open(TOKEN_PATH, "r") as f_old, open(backup_path, "w") as f_bk:
                        f_bk.write(f_old.read())
                    print(f"已备份旧凭证至: {backup_path}")
                except Exception as e:
                    print(f"备份旧凭证提示: {e}")

            # Save new token
            with open(TOKEN_PATH, "w") as f:
                json.dumps(payload, f, indent=2)
                f.write(json.dumps(payload, indent=2))
            os.chmod(TOKEN_PATH, 0o600)

            print(f"新凭据已保存至: {TOKEN_PATH}")
            print("重启 agycli2api 服务生效: systemctl restart agycli2api.service")
            return

        err_body = token_resp.json()
        error = err_body.get("error")
        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        elif error == "slow_down":
            interval += 2
            print("s", end="", flush=True)
            continue
        elif error == "expired_token":
            print("\n[错误] 授权码已过期，请重新运行脚本。")
            sys.exit(1)
        elif error == "access_denied":
            print("\n[错误] 用户拒绝了授权请求。")
            sys.exit(1)
        else:
            print(f"\n[错误] 获取 Token 失败: {err_body}")
            sys.exit(1)

    print("\n[超时] 授权超时，请重试。")
    sys.exit(1)


if __name__ == "__main__":
    main()
