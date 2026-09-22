"""Isolated Skland protocol; no raw responses or secrets are logged/cached."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .operator_box import ModuleProgress, Operator, OperatorBox, SkillProgress
from .sources import SourceError, _load_json_bytes
from .util import isoformat, utc_now

AS = "https://as.hypergryph.com"
ZONAI = "https://zonai.skland.com"
UA = "Skland/1.32.1 (com.hypergryph.skland; build:103201004; Android 33; ) Okhttp/4.11.0"
MAX_BODY = 8 * 1024 * 1024
# Exact route allowlist: this client cannot sign in for rewards or mutate game state.
ROUTES = {
    (AS, "/general/v1/send_phone_code"): "POST",
    (AS, "/user/auth/v2/token_by_phone_code"): "POST",
    (AS, "/user/oauth2/v2/grant"): "POST",
    (ZONAI, "/api/v1/user/auth/generate_cred_by_code"): "POST",
    (ZONAI, "/api/v1/auth/refresh"): "GET",
    (ZONAI, "/api/v1/game/player/binding"): "GET",
    (ZONAI, "/api/v1/game/player/info"): "GET",
}


class BoxError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def protocol_error() -> BoxError:
    return BoxError("protocol", "森空岛响应结构不符合已验证协议；未更新 Box。")


def obj(value: Any) -> dict:
    if not isinstance(value, dict):
        raise protocol_error()
    return value


def array(value: Any) -> list:
    if not isinstance(value, list):
        raise protocol_error()
    return value


def text_field(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise protocol_error()
    return value


def identity(value: Any) -> str:
    value = text_field(value)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise protocol_error()
    return value


def skill_identity(value: Any) -> str:
    value = text_field(value)
    # Shared game skills use IDs such as skcom_atk_up[1]. Preserve the full ID.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}(?:\[[0-9]+\])?", value):
        raise protocol_error()
    return value


def uid_field(value: Any) -> str:
    value = text_field(value)
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise protocol_error()
    return value


def optional_int(value: Any, low: int, high: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise protocol_error()
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BoxError("protocol", "森空岛接口发生重定向；请求已停止。")


def request_json(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    parsed = urllib.parse.urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if ROUTES.get((origin, parsed.path)) != method or parsed.fragment:
        raise protocol_error()
    if parsed.query and parsed.path != "/api/v1/game/player/info":
        raise protocol_error()
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
        **headers,
    }, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise BoxError("authentication", "登录已失效，请重新运行 box-login。") from None
        if exc.code == 403:
            raise BoxError("permission", "接口拒绝访问；请在官方森空岛检查账号权限。") from None
        if 300 <= exc.code < 400:
            raise BoxError("protocol", "接口重定向被拒绝。") from None
        raise BoxError("network", "森空岛 HTTP 请求失败；未更新 Box。") from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        raise BoxError("network", "无法连接森空岛；未更新 Box。") from None
    if len(raw) > MAX_BODY:
        raise protocol_error()
    try:
        return obj(_load_json_bytes(raw))
    except (SourceError, ValueError):
        raise protocol_error() from None


def response_data(response: dict, *, auth: bool = False, empty: bool = False) -> dict:
    response = obj(response)
    codes = [response[k] for k in ("code", "status") if k in response]
    if not codes or any(type(c) is not int for c in codes) or len(set(codes)) != 1:
        raise protocol_error()
    code = codes[0]
    if code:
        if code == 10002 or (auth and code != 10000):
            raise BoxError("authentication", "认证失败（验证码、登录状态或风控）；请在官方森空岛检查后重试。")
        if code == 10000:
            raise BoxError("permission", "森空岛拒绝访问；请检查账号绑定和数据访问权限。")
        raise BoxError("protocol", "森空岛返回未识别的业务错误；已停止。")
    if empty and response.get("data") is None:
        return {}
    return obj(response.get("data"))


@dataclass(frozen=True)
class Credentials:
    cred: str = field(repr=False)
    signing_token: str = field(repr=False)


def signed_headers(credentials: Credentials, url: str, timestamp: int) -> dict:
    parsed = urllib.parse.urlsplit(url)
    signed = {"platform": "", "timestamp": str(timestamp), "dId": "", "vName": ""}
    message = parsed.path + parsed.query + str(timestamp) + json.dumps(signed, separators=(",", ":"))
    digest = hmac.new(credentials.signing_token.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"cred": credentials.cred, "sign": hashlib.md5(digest.encode()).hexdigest(), **signed}


class SklandClient:
    def __init__(self, transport: Callable = request_json, clock: Callable = time.time):
        self.transport = transport
        self.clock = clock

    def send_code(self, phone: str) -> None:
        if not re.fullmatch(r"1[0-9]{10}", phone):
            raise BoxError("input", "请输入 11 位中国大陆手机号。")
        response_data(self.transport("POST", AS + "/general/v1/send_phone_code", {},
                                     {"phone": phone, "type": 2}), auth=True, empty=True)

    def login(self, phone: str, code: str) -> str:
        if not re.fullmatch(r"1[0-9]{10}", phone) or not re.fullmatch(r"[0-9]{4,8}", code):
            raise BoxError("input", "手机号或验证码格式不正确。")
        data = response_data(self.transport("POST", AS + "/user/auth/v2/token_by_phone_code", {},
                                           {"phone": phone, "code": code}), auth=True)
        return text_field(data.get("token"))

    def authenticate(self, *, token: str | None = None, cred: str | None = None) -> Credentials:
        if (token is None) == (cred is None):
            raise BoxError("authentication", "需要一种本地登录凭据。")
        if token is not None:
            grant = response_data(self.transport("POST", AS + "/user/oauth2/v2/grant", {},
                                  {"appCode": "4ca99fa6b56cc2ba", "token": text_field(token), "type": 0}), auth=True)
            generated = response_data(self.transport("POST", ZONAI + "/api/v1/user/auth/generate_cred_by_code", {},
                                      {"code": text_field(grant.get("code")), "kind": 1}), auth=True)
            cred = text_field(generated.get("cred"))
        cred = text_field(cred)
        refreshed = response_data(self.transport("GET", ZONAI + "/api/v1/auth/refresh", {"cred": cred}), auth=True)
        return Credentials(cred, text_field(refreshed.get("token")))

    def get(self, credentials: Credentials, path: str) -> dict:
        url = ZONAI + path
        return response_data(self.transport("GET", url, signed_headers(credentials, url, int(self.clock()) - 1)))


def select_account(data: dict, requested_uid: str | None = None) -> str:
    accounts = []
    for app in array(obj(data).get("list")):
        app = obj(app)
        app_code = text_field(app.get("appCode"))
        if app_code != "arknights":
            continue
        for binding in array(app.get("bindingList")):
            binding = obj(binding)
            if type(binding.get("isOfficial")) is not bool or type(binding.get("isDelete")) is not bool:
                raise protocol_error()
            channel = text_field(binding.get("channelMasterId"))
            if not binding["isOfficial"] or binding["isDelete"] or channel != "1":
                continue
            uid = uid_field(binding.get("uid"))
            if uid in accounts:
                raise protocol_error()
            accounts.append(uid)
    if requested_uid is not None:
        requested_uid = uid_field(requested_uid)
        accounts = [uid for uid in accounts if uid == requested_uid]
    if not accounts:
        raise BoxError("no_official_account", "未找到已绑定的明日方舟国服官服角色。")
    if len(accounts) != 1:
        raise BoxError("account_selection", "存在多个官服角色，请使用 --uid 显式选择。")
    return accounts[0]


def normalize_player(data: dict, uid: str, fetched_at: str) -> OperatorBox:
    data = obj(data)
    if uid_field(obj(data.get("status")).get("uid")) != uid:
        raise BoxError("account_mismatch", "玩家响应与请求角色不一致；未更新 Box。")
    operators = {}
    for raw in array(data.get("chars")):
        raw = obj(raw)
        char_id = identity(raw.get("charId"))
        if char_id in operators:
            raise protocol_error()
        skills = None
        if raw.get("skills") is not None:
            skills = {}
            for skill in array(raw["skills"]):
                skill = obj(skill)
                key = skill_identity(skill.get("id"))
                if key in skills:
                    raise protocol_error()
                skills[key] = SkillProgress(optional_int(skill.get("specializeLevel"), 0, 3))
        modules = None
        if raw.get("equip") is not None:
            modules = {}
            for module in array(raw["equip"]):
                module = obj(module)
                key = identity(module.get("id"))
                if key in modules:
                    raise protocol_error()
                locked = module.get("locked")
                if locked is not None and type(locked) is not bool:
                    raise protocol_error()
                modules[key] = ModuleProgress(optional_int(module.get("level"), 0, 3),
                                              None if locked is None else not locked)
        potential = optional_int(raw.get("potentialRank"), 0, 5)
        operators[char_id] = Operator(char_id, optional_int(raw.get("evolvePhase"), 0, 2),
                                     optional_int(raw.get("level"), 1, 90),
                                     None if potential is None else potential + 1,
                                     optional_int(raw.get("mainSkillLvl"), 1, 7), skills, modules)
    return OperatorBox("skland", fetched_at, "Official:" + uid, operators)


class SklandBoxProvider:
    def __init__(self, client: SklandClient, credentials: Credentials, uid: str | None = None):
        self.client = client
        self.credentials = credentials
        self.uid = uid

    def fetch_box(self) -> OperatorBox:
        uid = select_account(self.client.get(self.credentials, "/api/v1/game/player/binding"), self.uid)
        data = self.client.get(self.credentials, "/api/v1/game/player/info?" + urllib.parse.urlencode({"uid": uid}))
        return normalize_player(data, uid, isoformat(utc_now()))
