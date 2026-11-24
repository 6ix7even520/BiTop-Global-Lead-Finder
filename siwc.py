"""Official Sign in with ChatGPT (SIWC) client for the local BiTop Lead Finder.

Implements the documented open-source/local-client OAuth + PKCE flow.
Tokens are stored locally with Windows DPAPI (or Fernet on non-Windows dev hosts).
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jwt
from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent
AUTH_FILE = ROOT / "chatgpt_auth.bin"
HOST_FILE = ROOT / "chatgpt_host_id.txt"
SETTINGS_FILE = ROOT / "app_settings.json"
AUTH_LOCK = threading.RLock()
PENDING: dict[str, dict] = {}
AUTH_BASE = "https://auth.openai.com"
AUTHORIZE_URL = AUTH_BASE + "/api/accounts/authorize"
TOKEN_URL = AUTH_BASE + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
SHARING_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "BiTop Global Lead Finder"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_host_id() -> str:
    if HOST_FILE.exists():
        value = HOST_FILE.read_text(encoding="utf-8").strip()
        if value.startswith("urn:uuid:"):
            return value
    value = "urn:uuid:" + str(uuid.uuid4())
    HOST_FILE.write_text(value, encoding="utf-8")
    return value


def _fernet_key() -> bytes:
    key_file = ROOT / ".chatgpt_auth.key"
    if key_file.exists():
        return key_file.read_bytes()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def _dpapi_protect(raw: bytes) -> bytes:
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buf = ctypes.create_string_buffer(raw)
    source = DATA_BLOB(len(raw), ctypes.cast(source_buf, ctypes.POINTER(ctypes.c_byte)))
    output = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(ctypes.byref(source), "BiTop Global Lead Finder", None, None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 加密失败")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(raw: bytes) -> bytes:
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buf = ctypes.create_string_buffer(raw)
    source = DATA_BLOB(len(raw), ctypes.cast(source_buf, ctypes.POINTER(ctypes.c_byte)))
    output = DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 解密失败；请在原 Windows 用户账户下运行")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _protect(raw: bytes) -> bytes:
    if os.name == "nt":
        return b"DPAPI1" + _dpapi_protect(raw)
    return b"FERNET1" + Fernet(_fernet_key()).encrypt(raw)


def _unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"DPAPI1"):
        if os.name != "nt":
            raise ValueError("该 ChatGPT 凭据使用 Windows DPAPI 加密，请在原 Windows 电脑/账户中打开")
        return _dpapi_unprotect(blob[6:])
    if blob.startswith(b"FERNET1"):
        try:
            return Fernet(_fernet_key()).decrypt(blob[7:])
        except InvalidToken as exc:
            raise ValueError("ChatGPT 本地凭据解密失败；请重新登录") from exc
    raise ValueError("无法识别 ChatGPT 本地凭据格式")


def load_store() -> dict:
    if not AUTH_FILE.exists():
        return {"version": 2, "profiles": [], "active_profile_id": "", "selected_models": {}}
    try:
        data = json.loads(_unprotect(AUTH_FILE.read_bytes()).decode("utf-8"))
        # Migrate the early V1.2 single-profile format into the account-profile format.
        if "profiles" not in data:
            old = data.get("profile")
            profiles = []
            if old:
                old = dict(old)
                old.setdefault("profile_id", str(uuid.uuid4()))
                profiles.append(old)
            data = {"version": 2, "profiles": profiles,
                    "active_profile_id": profiles[0]["profile_id"] if profiles else "",
                    "selected_models": {profiles[0]["profile_id"]: data.get("selected_model", "")} if profiles else {}}
            save_store(data)
        data.setdefault("version", 2)
        data.setdefault("profiles", [])
        data.setdefault("active_profile_id", "")
        data.setdefault("selected_models", {})
        return data
    except Exception as exc:
        raise ValueError("读取本地 ChatGPT 授权失败：" + str(exc)) from exc


def _active_profile(store: dict):
    pid = store.get("active_profile_id", "")
    return next((p for p in store.get("profiles", []) if p.get("profile_id") == pid), None)


def _profile_by_id(store: dict, profile_id: str):
    return next((p for p in store.get("profiles", []) if p.get("profile_id") == profile_id), None)


def save_store(data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = _protect(raw)
    temp = AUTH_FILE.with_suffix(".tmp")
    temp.write_bytes(blob)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, AUTH_FILE)


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {"public_repo_url": ""}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {"public_repo_url": str(data.get("public_repo_url", ""))}
    except Exception:
        return {"public_repo_url": ""}


def save_settings(data: dict) -> dict:
    out = {"public_repo_url": str(data.get("public_repo_url", "")).strip()[:500]}
    SETTINGS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def verify_public_repo(repo_url: str) -> dict:
    """Require a public GitHub repo with a recognized open-source license before SIWC use."""
    p = urllib.parse.urlsplit((repo_url or "").strip())
    if p.scheme != "https" or p.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("Sign in with ChatGPT 的开源客户端方案要求公开源代码仓库。请先填写 GitHub 公共仓库链接。")
    parts = [x for x in p.path.strip("/").split("/") if x]
    if len(parts) < 2:
        raise ValueError("GitHub 仓库链接格式应为 https://github.com/用户名/仓库名")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    api_url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "BiTop-Lead-Finder/1.2.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("GitHub 未找到该公共仓库。请先创建并公开仓库，再填写链接。")
        raise ValueError(f"无法验证 GitHub 仓库（HTTP {exc.code}），请稍后重试。")
    except urllib.error.URLError:
        raise ValueError("无法连接 GitHub 验证公开仓库，请检查网络。")
    if info.get("private") is True:
        raise ValueError("该 GitHub 仓库是 Private。官方 SIWC 计划用量授权面向开源客户端，请先改为 Public。")
    lic = (info.get("license") or {}).get("spdx_id") or ""
    accepted = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0"}
    if lic not in accepted:
        raise ValueError("GitHub 仓库尚未识别到常见开源许可证。请在仓库加入 MIT、Apache-2.0 等开源许可证后重试。")
    contents_url = f"{api_url}/contents"
    req2 = urllib.request.Request(contents_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "BiTop-Lead-Finder/1.2.1"})
    try:
        with urllib.request.urlopen(req2, timeout=15) as resp:
            entries = json.load(resp)
    except Exception:
        raise ValueError("无法读取 GitHub 仓库文件列表，请确认仓库已公开且包含源代码和隐私说明。")
    root_files = {x.get("name") for x in entries if isinstance(x, dict)}
    required = {"app.py", "siwc.py", "index.html", "privacy.html", "requirements.txt", "LICENSE"}
    missing = sorted(required - root_files)
    if missing:
        raise ValueError("公开仓库缺少必要的客户端源文件：" + ", ".join(missing) + "。请先上传 V1.2 源代码。")
    return {"url": f"https://github.com/{owner}/{repo}", "license": lic, "name": info.get("full_name", f"{owner}/{repo}")}


def _post_form(url: str, fields: dict, timeout: int = 30, allow_empty: bool = False) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "BiTop-Lead-Finder/1.2.1"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if allow_empty and not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(2000).decode("utf-8", "ignore"))
            code = detail.get("error", "")
            desc = detail.get("error_description", "")
        except Exception:
            code, desc = "", ""
        msg = (code + " " + desc).strip()
        raise ValueError(f"OpenAI OAuth 返回 HTTP {exc.code}" + (f"：{msg[:240]}" if msg else "，请重试"))
    except urllib.error.URLError:
        raise ValueError("无法连接 OpenAI 授权服务，请检查网络")


def start_signin(repo_url: str, redirect_uri: str) -> str:
    repo = verify_public_repo(repo_url)
    store = load_store()
    # One active ChatGPT registration per local installation. Do not offer account rotation.
    profile = _active_profile(store)
    existing_client = str((profile or {}).get("client_id") or "")
    is_new = not existing_client
    client_id = "dynamic_agent_client" if is_new else existing_client
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(__import__("hashlib").sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": client_id,
        "ext_agent_host_id": _new_host_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if is_new:
        params["agent_name_hint"] = APP_NAME
    else:
        if profile.get("id_token"):
            params["id_token_hint"] = profile["id_token"]
        if profile.get("email"):
            params["login_hint"] = profile["email"]
    PENDING[state] = {
        "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
        "client_id": client_id, "is_new": is_new,
        "profile_id": (profile or {}).get("profile_id", ""),
        "existing_subject": (profile or {}).get("subject", ""), "repo": repo,
        "created_at": time.time(),
    }
    for old_state, item in list(PENDING.items()):
        if time.time() - item.get("created_at", 0) > 600:
            PENDING.pop(old_state, None)
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _discovery() -> dict:
    req = urllib.request.Request(AUTH_BASE + "/.well-known/openid-configuration", headers={"User-Agent": "BiTop-Lead-Finder/1.2.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as exc:
        raise ValueError("无法读取 OpenAI OIDC 配置，暂时无法验证登录身份：" + str(exc)) from exc
    if data.get("issuer") != AUTH_BASE or not data.get("jwks_uri"):
        raise ValueError("OpenAI OIDC 配置不符合预期，已中止授权")
    return data


def _validate_id_token(id_token: str, client_id: str, nonce: str) -> dict:
    if not id_token:
        raise ValueError("OpenAI 未返回 ID Token，无法验证账户身份")
    meta = _discovery()
    try:
        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg")
        if alg not in ("RS256", "ES256"):
            raise ValueError("ID Token 使用了不支持的签名算法")
        jwks = jwt.PyJWKClient(meta["jwks_uri"])
        signing_key = jwks.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, signing_key.key, algorithms=[alg], audience=client_id,
            issuer=meta["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]}, leeway=60)
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("ID Token nonce 不匹配，已拒绝登录")
        return claims
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("OpenAI ID Token 验证失败：" + str(exc)[:240]) from exc


def complete_callback(query: dict) -> dict:
    state = (query.get("state") or [""])[0]
    pending = PENDING.pop(state, None)
    if not state or not pending:
        raise ValueError("OAuth state 缺失、过期或不匹配。请重新点击 Continue with ChatGPT。")
    oauth_error = (query.get("error") or [""])[0]
    if oauth_error:
        desc = (query.get("error_description") or [""])[0]
        raise ValueError("ChatGPT 授权未完成：" + (desc or oauth_error)[:240])
    code = (query.get("code") or [""])[0]
    callback_client_id = (query.get("client_id") or [""])[0]
    if not code:
        raise ValueError("OpenAI 未返回授权码，请重新登录")
    if pending["is_new"]:
        issued_client_id = callback_client_id
        if not issued_client_id or issued_client_id == "dynamic_agent_client":
            raise ValueError("首次注册回调未返回正式 client_id，不能继续交换令牌")
    else:
        issued_client_id = pending["client_id"]
        if callback_client_id and callback_client_id != issued_client_id:
            raise ValueError("回调 client_id 与已注册账户不一致，已拒绝替换凭据")
    store = load_store()
    if pending["is_new"]:
        profile_id = str(uuid.uuid4())
        placeholder = {"profile_id": profile_id, "client_id": issued_client_id, "status": "registration_pending", "repo_url": pending["repo"]["url"], "label": "新 ChatGPT 账户"}
        store.setdefault("profiles", []).append(placeholder)
        store["active_profile_id"] = profile_id
        pending["profile_id"] = profile_id
        save_store(store)
    else:
        profile_id = pending.get("profile_id") or store.get("active_profile_id", "")
        placeholder = _profile_by_id(store, profile_id)
        if not placeholder:
            raise ValueError("找不到本次 OAuth 对应的本地账户档案，请重新登录")
    token_data = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": issued_client_id,
        "code": code, "code_verifier": pending["verifier"],
        "redirect_uri": pending["redirect_uri"], "resource": RESOURCE,
    }, timeout=40)
    id_token = token_data.get("id_token", "")
    claims = _validate_id_token(id_token, issued_client_id, pending["nonce"])
    subject = str(claims.get("sub", ""))
    if pending.get("existing_subject") and subject != pending["existing_subject"]:
        raise ValueError("本次登录账户与该 client_id 原绑定账户不一致。请不要覆盖原凭据。")
    scopes = str(token_data.get("scope", "")).split()
    # Plan-use permission must come from the token response, not from the authorization
    # redirect query string. A missing granted scope is treated as not authorized.
    expires_in = int(token_data.get("expires_in") or 3600)
    profile = {
        "profile_id": profile_id,
        "client_id": issued_client_id,
        "subject": subject,
        "email": claims.get("email", ""),
        "name": claims.get("name", claims.get("preferred_username", "")),
        "label": claims.get("email") or claims.get("name") or "ChatGPT 账户",
        "id_token": id_token,
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_at": time.time() + expires_in,
        "scopes": scopes,
        "status": "connected",
        "repo_url": pending["repo"]["url"],
        "license": pending["repo"]["license"],
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    if not profile["access_token"]:
        raise ValueError("OpenAI 未返回 access_token，无法调用模型")
    store = load_store()
    existing = _profile_by_id(store, profile_id)
    if existing:
        existing.update(profile)
    else:
        store.setdefault("profiles", []).append(profile)
    store["active_profile_id"] = profile_id
    store.setdefault("selected_models", {})[profile_id] = ""
    save_store(store)
    return {"email": profile["email"], "name": profile["name"], "profile_id": profile_id,
            "plan_usage_enabled": SHARING_SCOPE in scopes, "scope_missing": SHARING_SCOPE not in scopes}

def status() -> dict:
    store = load_store()
    p = _active_profile(store) or {}
    scopes = p.get("scopes") or []
    connected = bool(p.get("access_token")) and p.get("status") == "connected"
    accounts = ([{"profile_id": p.get("profile_id", ""), "label": p.get("label") or p.get("email") or "ChatGPT 账户",
                  "email": p.get("email", ""), "connected": connected,
                  "plan_usage_enabled": SHARING_SCOPE in scopes and connected}] if p else [])
    return {
        "connected": connected,
        "email": p.get("email", ""),
        "name": p.get("name", ""),
        "profile_id": p.get("profile_id", ""),
        "accounts": accounts,
        "plan_usage_enabled": connected and SHARING_SCOPE in scopes,
        "scope_missing": connected and SHARING_SCOPE not in scopes,
        "selected_model": store.get("selected_models", {}).get(p.get("profile_id", ""), ""),
        "repo_url": p.get("repo_url", "") or load_settings().get("public_repo_url", ""),
        "manage_usage_url": "https://chatgpt.com/settings/usage",
    }


def _refresh_locked(store: dict, profile: dict) -> dict:
    refresh = profile.get("refresh_token")
    if not refresh:
        raise ValueError("ChatGPT 登录已过期且没有 refresh_token，请重新登录")
    data = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token", "client_id": profile["client_id"],
        "refresh_token": refresh, "resource": RESOURCE,
    }, timeout=40)
    profile["access_token"] = data.get("access_token", "")
    profile["refresh_token"] = data.get("refresh_token") or refresh
    profile["token_type"] = data.get("token_type", "Bearer")
    profile["expires_at"] = time.time() + int(data.get("expires_in") or 3600)
    if data.get("scope"):
        profile["scopes"] = data["scope"].split()
    if not profile["access_token"]:
        raise ValueError("刷新授权未返回 access_token，请重新登录")
    existing = _profile_by_id(store, profile.get("profile_id", ""))
    if existing:
        existing.update(profile)
    save_store(store)
    return profile


def get_access_token() -> str:
    with AUTH_LOCK:
        store = load_store()
        profile = _active_profile(store) or {}
        if profile.get("status") != "connected" or not profile.get("access_token"):
            raise ValueError("请先点击 Continue with ChatGPT 登录")
        if SHARING_SCOPE not in (profile.get("scopes") or []):
            raise ValueError("你已登录 ChatGPT，但未授权 chatgpt.tokens.use.direct 计划用量权限。请重新连接并同意计划用量授权。")
        if float(profile.get("expires_at", 0)) <= time.time() + 90:
            profile = _refresh_locked(store, profile)
        return profile["access_token"]


def list_models() -> list[dict]:
    token = get_access_token()
    req = urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "BiTop-Lead-Finder/1.2.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"读取 ChatGPT 模型列表失败（HTTP {exc.code}）。请重新登录或检查计划用量授权。")
    except urllib.error.URLError:
        raise ValueError("无法连接 OpenAI 模型目录，请检查网络")
    models = data.get("models", [])
    visible = []
    for m in models:
        if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug"):
            visible.append({"slug": m["slug"], "display_name": m.get("display_name") or m["slug"]})
    if not visible:
        raise ValueError("账户模型目录未返回可显示的 models。请确认 ChatGPT 计划用量权限和账户可用模型。")
    return visible


def set_selected_model(model: str) -> str:
    model = str(model or "").strip()
    if not model:
        raise ValueError("请选择一个可用模型")
    allowed = {m["slug"] for m in list_models()}
    if model not in allowed:
        raise ValueError("所选模型不在当前 ChatGPT 账户可用模型列表中，请刷新模型列表")
    with AUTH_LOCK:
        store = load_store()
        profile = _active_profile(store)
        if not profile:
            raise ValueError("请先连接 ChatGPT")
        store.setdefault("selected_models", {})[profile.get("profile_id", "")] = model
        save_store(store)
    return model


def responses_text(model: str, instructions: str, user_text: str) -> str:
    token = get_access_token()
    payload = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_text}],
        "store": False,
        "stream": True,
    }
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "BiTop-Lead-Finder/1.2.1"}, method="POST")
    chunks = []
    completed = False
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_line = line[5:].strip()
                if not data_line or data_line == "[DONE]":
                    continue
                try:
                    event = json.loads(data_line)
                except json.JSONDecodeError:
                    continue
                et = event.get("type", "")
                if et == "response.output_text.delta":
                    chunks.append(str(event.get("delta", "")))
                elif et == "response.failed":
                    err = (event.get("response") or {}).get("error") or {}
                    code = err.get("code", "unknown_error")
                    if code in ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"):
                        raise ValueError("ChatGPT 计划用量暂不可用或已达到使用限制。请打开 ChatGPT Usage 设置查看限额。")
                    raise ValueError("ChatGPT Responses 请求失败：" + str(code))
                elif et == "response.incomplete":
                    raise ValueError("ChatGPT 响应未完成，请缩短背调材料后重试")
                elif et == "response.completed":
                    completed = True
                    if not chunks:
                        response = event.get("response") or {}
                        for item in response.get("output", []):
                            for content in item.get("content", []):
                                if content.get("type") == "output_text":
                                    chunks.append(content.get("text", ""))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1200).decode("utf-8", "ignore")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise ValueError("ChatGPT OAuth 授权无效或缺少计划用量权限，请重新连接 ChatGPT。")
        raise ValueError(f"ChatGPT Responses API 返回 HTTP {exc.code}：{detail[:220]}")
    except urllib.error.URLError:
        raise ValueError("无法连接 OpenAI Responses API，请检查网络")
    if not completed:
        raise ValueError("ChatGPT 流式响应未收到 response.completed，不能视为成功。请重试。")
    result = "".join(chunks).strip()
    if not result:
        raise ValueError("ChatGPT 已完成响应，但没有返回可解析文本")
    return result


def disconnect() -> dict:
    with AUTH_LOCK:
        store = load_store()
        profile = _active_profile(store) or {}
        refresh = profile.get("refresh_token")
        remote_revoked = False
        if refresh and profile.get("client_id"):
            try:
                meta = _discovery()
                endpoint = meta.get("revocation_endpoint")
                if endpoint:
                    revoke_fields = {"token": refresh, "token_type_hint": "refresh_token", "client_id": profile["client_id"]}
                    for attempt in range(2):
                        try:
                            _post_form(endpoint, revoke_fields, timeout=20, allow_empty=True)
                            remote_revoked = True
                            break
                        except Exception:
                            if attempt == 0:
                                time.sleep(1)
            except Exception:
                remote_revoked = False
        if profile:
            profile.update({"status": "disconnected", "access_token": "", "refresh_token": "", "id_token": "", "scopes": [], "expires_at": 0})
            existing = _profile_by_id(store, profile.get("profile_id", ""))
            if existing:
                existing.update(profile)
            store.setdefault("selected_models", {})[profile.get("profile_id", "")] = ""
            save_store(store)
        return {"ok": True, "remote_revoked": remote_revoked}
