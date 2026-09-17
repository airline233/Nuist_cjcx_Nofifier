"""
NUIST 统一身份认证登录模块（Passkey，纯网络层，不启动浏览器）
支持普通模式和 VPN 模式

调用方式与旧版基于 Playwright 的实现一致，只把第二个参数从密码换成
Passkey bundle：

    from NuistLogin import NuistLogin

    cookies = NuistLogin("202512345678", "passkey.json", service).login()

bundle 由 browser_passkey.js 导出，可以传文件路径、JSON 文本，或已解析的
dict，需包含 rpId / credentialId / 私钥，以及 userId 和 anonbiometricsd。
旧版 Playwright 实现保留在 legacy/playwright/NuistLogin.py，仅供参考。

本模块自成一体，只依赖 requests 和 cryptography。
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from enum import IntEnum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


# ==================== 日志级别 ====================

class LogLevel(IntEnum):
    TRACE = 0
    INFO = 1
    ERROR = 2


# ==================== 自定义异常 ====================

class CaptchaError(Exception):
    """保留以兼容旧代码；Passkey 登录不涉及验证码，不会被抛出"""
    pass


class CredentialError(Exception):
    """bundle 不可用，或服务端拒绝了本次 Passkey 断言"""
    pass


class LoginError(Exception):
    """通用登录错误"""
    pass


class _VpnSessionExpired(LoginError):
    """内部信号：被重定向到 VPN 的 SSO 登录页，说明 VPN cookies 失效"""
    pass


# ==================== URL 配置 ====================

AUTHSERVER_NORMAL = "https://authserver.nuist.edu.cn"
AUTHSERVER_VPN = "https://client.vpn.nuist.edu.cn/https/webvpnf971ba19a2d1e2ef80f6438b88af30111008953ef2e157f5c5904ebc57eef098"
VPN_DOMAIN = "client.vpn.nuist.edu.cn"
VPN_SSO_LOGIN_PATH = "/enlink/sso/login"
VPN_CAS_CALLBACK = "https://client.vpn.nuist.edu.cn/enlink/api/client/callback/cas"

LOGIN_PATH = "/authserver/login"
START_ASSERTION_PATH = "/authserver/startAssertion"

# clientDataJSON 里的 origin 必须固定为真实 authserver：即使经 webvpn 代理，
# 填代理域名会被服务端以 401 拒绝。
WEBAUTHN_ORIGIN = AUTHSERVER_NORMAL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0"
)
REDIRECT_CODES = (301, 302, 303, 307, 308)


# ==================== 主类 ====================

class NuistLogin:
    """NUIST 统一身份认证登录器（Passkey）"""

    TIMEOUT = 30  # 单次请求超时 (s)

    def __init__(
        self,
        username: str,
        passkey: str | Path | dict[str, Any],
        service: str,
        headless: bool = True,
        log_level: LogLevel = LogLevel.ERROR,
        use_vpn: bool = False,
        vpn_cookies: dict = None
    ):
        """
        初始化登录器

        :param username: 学号；bundle 内已含 userId 时可留空
        :param passkey: Passkey bundle，文件路径 / JSON 文本 / dict
        :param service: 登录成功后跳转的服务 URL（始终使用原始 URL）
        :param headless: 兼容旧调用而保留，不起作用（不启动浏览器）
        :param log_level: 日志级别
        :param use_vpn: 是否使用 VPN 模式
        :param vpn_cookies: VPN cookies 字典（可选，无则自动获取）
        """
        self.username = username
        self.passkey = passkey
        self.service = service
        self.headless = headless
        self.log_level = log_level
        self.use_vpn = use_vpn
        self.vpn_cookies = vpn_cookies or {}
        self.session: requests.Session | None = None

    # ==================== 日志 ====================

    def _log(self, level: LogLevel, message: str):
        """分级日志输出"""
        if level >= self.log_level:
            prefix = {
                LogLevel.TRACE: "[-]",
                LogLevel.INFO: "[*]",
                LogLevel.ERROR: "[!]"
            }
            print(f"{prefix.get(level, '[?]')} {message}")

    # ==================== 凭据加载 ====================

    def _load_bundle(self) -> dict[str, Any]:
        """bundle 可以是 dict、JSON 文本或 JSON 文件路径"""
        source = self.passkey
        if isinstance(source, dict):
            data: Any = source
        else:
            text = str(source).strip()
            if text.startswith("{"):
                raw = text
            else:
                try:
                    raw = Path(source).read_text(encoding="utf-8")
                except OSError as e:
                    raise CredentialError(f"读取 bundle 失败: {e}") from e
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise CredentialError(f"解析 bundle 失败: {e}") from e

        if not isinstance(data, dict):
            raise CredentialError("bundle 必须是 JSON 对象")
        for name in ("rpId", "credentialId"):
            if not data.get(name):
                raise CredentialError(f"bundle 缺少字段: {name}")
        if not data.get("privateKeyPkcs8Pem") and not data.get("privateKeyJwk"):
            raise CredentialError("bundle 缺少 privateKeyPkcs8Pem/privateKeyJwk")
        return data

    @staticmethod
    def _load_private_key(bundle: dict) -> ec.EllipticCurvePrivateKey:
        """加载 bundle 中的 ES256 私钥"""
        try:
            if bundle.get("privateKeyPkcs8Pem"):
                key = serialization.load_pem_private_key(
                    bundle["privateKeyPkcs8Pem"].encode("ascii"), password=None
                )
            else:
                jwk = bundle["privateKeyJwk"]
                scalar = int.from_bytes(_b64url_decode(jwk["d"]), "big")
                key = ec.derive_private_key(scalar, ec.SECP256R1())
        except (KeyError, ValueError, TypeError, base64.binascii.Error) as e:
            raise CredentialError(f"passkey 私钥无法解析: {e}") from e

        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise CredentialError("passkey 私钥必须是 P-256/ES256")
        return key

    def _resolve_ids(self, bundle: dict) -> tuple[str, str]:
        """返回 (userId, startId)；userId 是学号的 Base64URL 形式"""
        user_id = bundle.get("userId")
        if not user_id and self.username:
            user_id = _b64url_encode(self.username.encode("utf-8"))
        if not user_id:
            raise CredentialError("缺少 userId：请使用新 bundle，或传入 username")

        start_id = bundle.get("anonbiometricsd")
        if not start_id:
            raise CredentialError("缺少 anonbiometricsd：请使用新 bundle")
        return user_id, start_id

    # ==================== 会话与 URL ====================

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,en;q=0.9,en-US;q=0.8",
        })
        return session

    @staticmethod
    def _login_url(base: str, service: str) -> str:
        """拼登录页 URL；base 为普通或 webvpn 代理前缀"""
        url = f"{base}{LOGIN_PATH}"
        if not service:
            return url
        return f"{url}?service={requests.utils.quote(service, safe=':/')}"

    @staticmethod
    def _http_origin(base: str) -> str:
        """HTTP 请求头里的 Origin，与实际访问的主机一致"""
        parts = urlsplit(base)
        return f"{parts.scheme}://{parts.netloc}"

    # ==================== CAS + WebAuthn 流程 ====================

    def _open_login_page(self, session: requests.Session, login_url: str) -> tuple[str | None, str]:
        """
        访问登录页，取得会话 Cookie 和 execution 令牌

        :return: (execution, 落地 URL)；已登录自动跳转时 execution 为 None
        """
        response = session.get(
            login_url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            timeout=self.TIMEOUT,
        )
        response.raise_for_status()

        # SSO 已登录时会直接跳走，不再需要提交断言
        if LOGIN_PATH not in response.url:
            return None, response.url

        # execution 位于登录页隐藏 input；抓包值 e1s1 作为后备
        match = re.search(
            r'name=["\']execution["\'][^>]*value=["\']([^"\']+)',
            html.unescape(response.text),
            re.IGNORECASE,
        )
        execution = match.group(1) if match else "e1s1"
        self._log(LogLevel.TRACE, f"execution={execution}")
        return execution, response.url

    def _start_assertion(
        self,
        session: requests.Session,
        base: str,
        login_url: str,
        user_id: str,
        start_id: str,
    ) -> dict:
        """请求 WebAuthn 断言参数（challenge 等）"""
        response = session.post(
            f"{base}{START_ASSERTION_PATH}",
            json={"userId": user_id, "id": start_id},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json;charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self._http_origin(base),
                "Referer": login_url,
            },
            timeout=self.TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("success") is False:
            raise CredentialError(f"startAssertion 失败: {body}")

        request = body.get("result", {}).get("request")
        if not isinstance(request, dict):
            request = body.get("datas", {}).get("request")
        if not isinstance(request, dict) or not request.get("requestId"):
            raise LoginError(f"startAssertion 响应中没有有效的 request: {body}")
        return request

    def _make_assertion(
        self,
        request_data: dict,
        bundle: dict,
        private_key: ec.EllipticCurvePrivateKey,
    ) -> dict:
        """用 bundle 里的私钥离线完成 WebAuthn 断言签名"""
        options = request_data.get("publicKeyCredentialRequestOptions", {})
        challenge = options.get("challenge")
        rp_id = options.get("rpId") or bundle["rpId"]
        credential_id = bundle["credentialId"]
        if not challenge or not rp_id:
            raise LoginError("startAssertion 缺少 challenge/rpId")

        allowed = options.get("allowCredentials") or []
        if allowed and credential_id not in {
            item.get("id") for item in allowed if isinstance(item, dict)
        }:
            raise CredentialError(
                "bundle 中的 credentialId 不在 allowCredentials 中，"
                "该 Passkey 可能已被吊销"
            )

        client_data_json = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": challenge,
                "origin": WEBAUTHN_ORIGIN,
                "crossOrigin": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        # 前端要求 userVerification，故 flags = UP(0x01) | UV(0x04)；计数器固定 0
        authenticator_data = (
            hashlib.sha256(rp_id.encode("utf-8")).digest()
            + b"\x05"
            + b"\x00\x00\x00\x00"
        )
        # WebAuthn ES256 签名为 DER 编码
        signature = private_key.sign(
            authenticator_data + hashlib.sha256(client_data_json).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "type": "public-key",
            "id": credential_id,
            "response": {
                "authenticatorData": _b64url_encode(authenticator_data),
                "clientDataJSON": _b64url_encode(client_data_json),
                "signature": _b64url_encode(signature),
            },
            "clientExtensionResults": {"appid": False},
        }

    def _submit_login(
        self,
        session: requests.Session,
        base: str,
        login_url: str,
        user_id: str,
        request_data: dict,
        credential: dict,
        execution: str,
    ) -> requests.Response:
        """提交断言到 CAS 登录表单"""
        response_json = json.dumps(
            {
                "requestId": request_data["requestId"],
                "credential": credential,
                "sessionToken": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return session.post(
            login_url,
            data={
                "_eventId": "submit",
                # username 字段发的是 Base64URL 的 userId，不是明文学号，
                # 传明文学号会被服务端以 401 拒绝。
                "username": user_id,
                "responseJson": response_json,
                "cllt": "fidoLogin",
                "dllt": "generalLogin",
                "lt": "",
                "execution": execution,
            },
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self._http_origin(base),
                "Referer": login_url,
                "Upgrade-Insecure-Requests": "1",
            },
            allow_redirects=False,
            timeout=self.TIMEOUT,
        )

    def _passkey_login(
        self,
        session: requests.Session,
        base: str,
        service: str,
        bundle: dict,
        private_key: ec.EllipticCurvePrivateKey,
        user_id: str,
        start_id: str,
    ) -> str:
        """
        在指定 authserver（普通或 webvpn 代理）上完成一次 Passkey 登录

        :return: 跟随跳转后的落地 URL
        """
        login_url = self._login_url(base, service)
        self._log(LogLevel.TRACE, f"访问: {login_url[:80]}...")
        execution, landing = self._open_login_page(session, login_url)
        if execution is None:
            _reject_vpn_sso_page(landing)
            self._log(LogLevel.INFO, "SSO 已登录，自动跳转")
            return landing

        self._log(LogLevel.INFO, "提交 Passkey 断言...")
        request_data = self._start_assertion(session, base, login_url, user_id, start_id)
        credential = self._make_assertion(request_data, bundle, private_key)
        response = self._submit_login(
            session, base, login_url, user_id, request_data, credential, execution
        )
        if response.status_code not in REDIRECT_CODES:
            raise CredentialError(
                f"登录未返回重定向: HTTP {response.status_code}；"
                "Passkey 可能已失效，请重新注册"
            )

        location = response.headers.get("Location", "")
        if not location:
            raise LoginError("登录返回重定向但缺少 Location")

        landed = session.get(
            urljoin(login_url, location), allow_redirects=True, timeout=self.TIMEOUT
        )
        self._log(LogLevel.TRACE, f"落地页: {landed.url[:100]}")
        _reject_vpn_sso_page(landed.url)
        if LOGIN_PATH in landed.url:
            raise LoginError(f"登录后又跳回认证页，service 可能不正确: {service}")
        return landed.url

    # ==================== VPN 模式 ====================

    @staticmethod
    def _extract_vpn_cookies(session: requests.Session) -> dict:
        """从会话中提取 VPN 域名的 cookies"""
        return {
            cookie.name: cookie.value
            for cookie in session.cookies
            if VPN_DOMAIN in (cookie.domain or "")
        }

    def _load_vpn_cookies(self, session: requests.Session):
        """将 VPN cookies 写入会话"""
        self._log(LogLevel.TRACE, "加载 VPN cookies...")
        for name, value in self.vpn_cookies.items():
            session.cookies.set(name, value, domain=VPN_DOMAIN, path="/")

    def _clear_vpn_cookies(self, session: requests.Session):
        for cookie in list(session.cookies):
            if VPN_DOMAIN in (cookie.domain or ""):
                session.cookies.clear(cookie.domain, cookie.path, cookie.name)

    def _acquire_vpn_cookies(
        self,
        session: requests.Session,
        bundle: dict,
        private_key: ec.EllipticCurvePrivateKey,
        user_id: str,
        start_id: str,
    ):
        """在普通网络下登录 VPN 的 CAS 回调，换取 VPN cookies"""
        self._log(LogLevel.INFO, "获取 VPN cookies...")
        self._clear_vpn_cookies(session)
        self._passkey_login(
            session, AUTHSERVER_NORMAL, VPN_CAS_CALLBACK, bundle, private_key,
            user_id, start_id,
        )
        self.vpn_cookies = self._extract_vpn_cookies(session)
        if not self.vpn_cookies:
            raise LoginError("登录成功但未获取到 VPN cookies")
        self._log(LogLevel.INFO, f"已获取 VPN cookies ({len(self.vpn_cookies)} 个)")

    def _login_vpn(
        self,
        session: requests.Session,
        bundle: dict,
        private_key: ec.EllipticCurvePrivateKey,
        user_id: str,
        start_id: str,
    ):
        """
        VPN 模式登录流程

        VPN cookies 是否有效无法靠预先探测判断（webvpn 对失效会话也会返回
        200 的访客页），因此直接拿现有 cookies 试一次，被踢回 SSO 登录页
        再重新获取。
        """
        if self.vpn_cookies:
            self._load_vpn_cookies(session)
            try:
                self._passkey_login(
                    session, AUTHSERVER_VPN, self.service, bundle, private_key,
                    user_id, start_id,
                )
                return
            except _VpnSessionExpired:
                self._log(LogLevel.INFO, "VPN cookies 已失效，重新获取...")

        self._acquire_vpn_cookies(session, bundle, private_key, user_id, start_id)
        self._passkey_login(
            session, AUTHSERVER_VPN, self.service, bundle, private_key,
            user_id, start_id,
        )

    # ==================== 对外接口 ====================

    def login(self) -> dict:
        """
        执行登录流程

        :return: 登录成功后的 cookies 字典
        :raises CredentialError: bundle 不可用或 Passkey 被拒绝
        :raises LoginError: 其他登录错误
        """
        bundle = self._load_bundle()
        private_key = self._load_private_key(bundle)
        user_id, start_id = self._resolve_ids(bundle)

        session = self._new_session()
        if self.use_vpn:
            self._log(LogLevel.INFO, "VPN 模式登录...")
            self._login_vpn(session, bundle, private_key, user_id, start_id)
        else:
            self._log(LogLevel.INFO, "普通模式登录...")
            self._passkey_login(
                session, AUTHSERVER_NORMAL, self.service, bundle, private_key,
                user_id, start_id,
            )
        self._log(LogLevel.INFO, "登录成功")

        self.session = session
        return session.cookies.get_dict()


# ==================== 内部工具 ====================

def _reject_vpn_sso_page(url: str):
    """落到 VPN 的 SSO 登录页说明 VPN 会话失效，而不是登录成功"""
    if VPN_DOMAIN in url and VPN_SSO_LOGIN_PATH in url:
        raise _VpnSessionExpired(f"被重定向到 VPN 登录页: {url[:100]}")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    value = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4))


# ==================== 命令行入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python NuistLogin.py <学号> <passkey.json> [--vpn] [--vpn-cookies vpn_cookies.json]")
        print("示例:")
        print("  python NuistLogin.py 202512345678 passkey.json")
        print("  python NuistLogin.py 202512345678 passkey.json --vpn")
        print("  python NuistLogin.py 202512345678 passkey.json --vpn --vpn-cookies vpn_cookies.json")
        sys.exit(1)

    user = sys.argv[1]
    bundle_path = sys.argv[2]
    use_vpn = "--vpn" in sys.argv

    # 加载可选的 VPN cookies
    vpn_cookies = None
    if "--vpn-cookies" in sys.argv:
        idx = sys.argv.index("--vpn-cookies")
        if idx + 1 < len(sys.argv):
            vpn_cookies_file = sys.argv[idx + 1]
            try:
                with open(vpn_cookies_file, "r") as f:
                    vpn_cookies = json.load(f)
                print(f"[*] 已加载 VPN cookies: {vpn_cookies_file}")
            except FileNotFoundError:
                print(f"[!] VPN cookies 文件不存在: {vpn_cookies_file}")

    try:
        service = "https://jwxt.nuist.edu.cn/jwapp/sys/emaphome/portal/index.do"

        bot = NuistLogin(
            username=user,
            passkey=bundle_path,
            service=service,
            headless=False,
            log_level=LogLevel.INFO,
            use_vpn=use_vpn,
            vpn_cookies=vpn_cookies
        )

        cookies = bot.login()

        print("\n[SUCCESS] 获取到的 Cookies:")
        for name, value in cookies.items():
            print(f"  {name}: {value[:20]}..." if len(value) > 20 else f"  {name}: {value}")

        with open("nuist_cookies.json", "w") as f:
            json.dump(cookies, f)
        print("\n[*] Cookies 已保存到 nuist_cookies.json")

        # VPN 模式下也保存 VPN cookies 供下次使用
        if use_vpn and bot.vpn_cookies:
            with open("vpn_cookies.json", "w") as f:
                json.dump(bot.vpn_cookies, f)
            print("[*] VPN Cookies 已保存到 vpn_cookies.json")

    except CredentialError as e:
        print(f"\n[CREDENTIAL ERROR] {e}")
        sys.exit(3)
    except CaptchaError as e:
        print(f"\n[CAPTCHA ERROR] {e}")
        sys.exit(4)
    except LoginError as e:
        print(f"\n[LOGIN ERROR] {e}")
        sys.exit(5)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
