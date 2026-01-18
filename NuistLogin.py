"""
NUIST 统一身份认证登录模块
支持普通模式和 VPN 模式
"""

import time
import re
from enum import IntEnum
from playwright.sync_api import sync_playwright, Page, BrowserContext
import ddddocr


# ==================== 日志级别 ====================

class LogLevel(IntEnum):
    TRACE = 0
    INFO = 1
    ERROR = 2


# ==================== 自定义异常 ====================

class CaptchaError(Exception):
    """验证码识别或验证失败"""
    pass


class CredentialError(Exception):
    """用户名或密码错误"""
    pass


class LoginError(Exception):
    """通用登录错误"""
    pass


# ==================== URL 配置 ====================

AUTHSERVER_NORMAL = "https://authserver.nuist.edu.cn"
AUTHSERVER_VPN = "https://client.vpn.nuist.edu.cn/https/webvpnf971ba19a2d1e2ef80f6438b88af30111008953ef2e157f5c5904ebc57eef098"
VPN_DOMAIN = "client.vpn.nuist.edu.cn"
VPN_SSO_LOGIN_PATH = "/enlink/sso/login"
VPN_CAS_CALLBACK = "https://client.vpn.nuist.edu.cn/enlink/api/client/callback/cas"


# ==================== 页面选择器 ====================

class Selectors:
    """页面元素选择器集中管理"""
    LOGIN_FORM = "#pwdFromId"
    USERNAME = "#username"
    PASSWORD = "#password"
    CAPTCHA_DIV = "#captchaDiv"
    CAPTCHA_IMG = "#captchaImg"
    CAPTCHA_INPUT = "#captcha"
    CAPTCHA_REFRESH = ".captcha-refresh"
    REMEMBER_ME = "#rememberMe"
    LOGIN_BUTTON = "#login_submit"
    ERROR_TIP = "#showErrorTip"


# ==================== 主类 ====================

class NuistLogin:
    """NUIST 统一身份认证登录器"""
    
    # 常量配置
    MAX_CAPTCHA_RETRIES = 10      # 验证码识别最大重试次数
    MAX_LOGIN_RETRIES = 3         # 登录最大重试次数（验证码错误时）
    PAGE_LOAD_TIMEOUT = 30000     # 页面加载超时 (ms)
    LOGIN_REDIRECT_TIMEOUT = 8000 # 登录跳转超时 (ms)
    
    def __init__(
        self,
        username: str,
        password: str,
        service: str,
        headless: bool = True,
        log_level: LogLevel = LogLevel.ERROR,
        use_vpn: bool = False,
        vpn_cookies: dict = None
    ):
        """
        初始化登录器
        
        :param username: 学号
        :param password: 密码
        :param service: 登录成功后跳转的服务 URL（始终使用原始 URL）
        :param headless: 是否使用无头浏览器
        :param log_level: 日志级别
        :param use_vpn: 是否使用 VPN 模式
        :param vpn_cookies: VPN cookies 字典（可选，无则自动获取）
        """
        self.username = username
        self.password = password
        self.service = service
        self.headless = headless
        self.log_level = log_level
        self.use_vpn = use_vpn
        self.vpn_cookies = vpn_cookies or {}
        
        # 初始化 OCR
        self.ocr = ddddocr.DdddOcr(show_ad=False)

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

    # ==================== 浏览器管理 ====================
    
    def _setup_browser_context(self, playwright) -> BrowserContext:
        """创建并配置浏览器上下文"""
        browser = playwright.chromium.launch(headless=self.headless)
        context = browser.new_context()
        return context
    
    def _load_vpn_cookies(self, context: BrowserContext):
        """将 VPN cookies 加载到浏览器上下文"""
        if self.vpn_cookies:
            self._log(LogLevel.TRACE, "加载 VPN cookies...")
            vpn_cookie_list = [
                {"name": name, "value": value, "domain": VPN_DOMAIN, "path": "/"}
                for name, value in self.vpn_cookies.items()
            ]
            context.add_cookies(vpn_cookie_list)
    
    def _extract_vpn_cookies(self, cookies: list) -> dict:
        """从 cookies 列表中提取 VPN 域名的 cookies"""
        return {
            item['name']: item['value'] 
            for item in cookies 
            if VPN_DOMAIN in item.get('domain', '')
        }

    # ==================== 页面检查 ====================
    
    def _is_vpn_login_redirect(self, page: Page) -> bool:
        """检查是否被重定向到 VPN 登录页"""
        return self.use_vpn and VPN_SSO_LOGIN_PATH in page.url

    def _is_on_login_page(self, page: Page) -> bool:
        """检查是否在 authserver 登录页"""
        return "authserver/login" in page.url

    def _wait_for_login_page(self, page: Page):
        """等待登录页面加载完成"""
        self._log(LogLevel.TRACE, "等待登录页面加载...")
        try:
            page.wait_for_selector(Selectors.LOGIN_FORM, timeout=self.PAGE_LOAD_TIMEOUT)
        except Exception as e:
            raise LoginError(f"登录页面加载失败: {e}")

    # ==================== 验证码处理 ====================
    
    def _is_captcha_visible(self, page: Page) -> bool:
        """检查验证码是否可见"""
        captcha_div = page.locator(Selectors.CAPTCHA_DIV)
        return captcha_div.is_visible()

    def _recognize_captcha(self, page: Page) -> str:
        """
        识别验证码，自动刷新直到获得有效格式
        
        :return: 4位字母数字验证码
        :raises CaptchaError: 多次尝试后仍无法获取有效验证码
        """
        for attempt in range(self.MAX_CAPTCHA_RETRIES):
            # 等待验证码图片加载
            page.wait_for_selector(Selectors.CAPTCHA_IMG, state="visible")
            img_bytes = page.locator(Selectors.CAPTCHA_IMG).screenshot()
            
            # OCR 识别
            code = self.ocr.classification(img_bytes)
            self._log(LogLevel.TRACE, f"验证码识别 [{attempt + 1}/{self.MAX_CAPTCHA_RETRIES}]: {code}")
            
            # 验证格式：4位字母数字
            if re.match(r'^[a-zA-Z0-9]{4}$', code):
                self._log(LogLevel.TRACE, f"验证码格式有效: {code}")
                return code
            
            # 格式无效，刷新验证码
            self._log(LogLevel.TRACE, "验证码格式无效，刷新中...")
            page.locator(Selectors.CAPTCHA_REFRESH).click()
            time.sleep(0.5)
        
        raise CaptchaError(f"验证码识别失败：连续 {self.MAX_CAPTCHA_RETRIES} 次未获取有效格式")

    def _fill_captcha(self, page: Page):
        """填写验证码（如果需要）"""
        time.sleep(0.3)  # 等待验证码区域状态稳定
        
        if self._is_captcha_visible(page):
            self._log(LogLevel.INFO, "检测到验证码，开始识别...")
            code = self._recognize_captcha(page)
            page.fill(Selectors.CAPTCHA_INPUT, code)
            self._log(LogLevel.TRACE, f"已填写验证码: {code}")
        else:
            self._log(LogLevel.TRACE, "无需验证码")

    # ==================== 表单操作 ====================
    
    def _fill_credentials(self, page: Page):
        """填写用户名和密码"""
        self._log(LogLevel.TRACE, "填写登录凭据...")
        page.fill(Selectors.USERNAME, self.username)
        page.fill(Selectors.PASSWORD, self.password)

    def _check_remember_me(self, page: Page):
        """勾选记住我（如果可见）"""
        remember_me = page.locator(Selectors.REMEMBER_ME)
        if remember_me.is_visible():
            remember_me.check()
            self._log(LogLevel.TRACE, "已勾选「记住我」")

    def _submit_login(self, page: Page):
        """提交登录表单"""
        self._log(LogLevel.TRACE, "提交登录表单...")
        page.click(Selectors.LOGIN_BUTTON)

    # ==================== 登录结果判断 ====================
    
    def _check_login_error(self, page: Page) -> str | None:
        """
        检查页面上的错误提示
        
        :return: 错误信息，如果没有错误返回 None
        """
        error_tip = page.locator(Selectors.ERROR_TIP)
        if error_tip.is_visible():
            error_msg = error_tip.inner_text().strip()
            if error_msg:
                return error_msg
        return None

    def _is_login_successful(self, page: Page) -> bool:
        """判断是否登录成功（已离开登录页面）"""
        current_url = page.url
        
        # 仍在登录页面
        if "authserver/login" in current_url:
            return False
        
        # VPN 模式：检查是否在 VPN 域名下（但不是 SSO 登录页）
        if self.use_vpn:
            return VPN_DOMAIN in current_url and VPN_SSO_LOGIN_PATH not in current_url
        
        # 普通模式：检查是否离开了 authserver
        return "authserver.nuist.edu.cn" not in current_url

    def _wait_for_redirect(self, page: Page):
        """
        等待登录后的页面跳转
        
        :raises CaptchaError: 验证码错误
        :raises CredentialError: 用户名或密码错误
        :raises LoginError: 其他登录错误
        """
        try:
            # 等待 URL 变化（离开登录页）
            page.wait_for_url(
                lambda url: "authserver/login" not in url,
                timeout=self.LOGIN_REDIRECT_TIMEOUT
            )
            self._log(LogLevel.TRACE, "页面已跳转")
            return
        except Exception:
            pass  # 超时，继续检查错误
        
        # 检查页面错误提示
        error_msg = self._check_login_error(page)
        if error_msg:
            if "图形动态码错误" in error_msg or "验证码" in error_msg:
                raise CaptchaError(f"验证码错误: {error_msg}")
            elif "用户名或者密码有误" in error_msg:
                raise CredentialError(f"凭据错误: {error_msg}")
            else:
                raise LoginError(f"登录失败: {error_msg}")
        
        # 再次检查是否实际上已成功
        if self._is_login_successful(page):
            self._log(LogLevel.TRACE, "登录成功")
            return
        
        # 未知状态
        raise LoginError(f"登录状态未知，当前 URL: {page.url}")

    # ==================== 核心登录流程 ====================
    
    def _do_login_attempt(self, page: Page, login_url: str) -> bool:
        """
        执行单次登录尝试
        
        :param page: 浏览器页面
        :param login_url: 登录 URL
        :return: True 如果需要登录并完成，False 如果已登录自动跳转
        :raises CaptchaError: 验证码错误（可重试）
        :raises CredentialError: 凭据错误（不可重试）
        :raises LoginError: 其他错误
        """
        self._log(LogLevel.TRACE, f"访问: {login_url[:80]}...")
        page.goto(login_url)
        
        # SSO cookies 持久化：如果已登录，会自动重定向到目标页面
        if not self._is_on_login_page(page):
            self._log(LogLevel.INFO, "SSO 已登录，自动跳转")
            return False
        
        # 等待页面加载
        self._wait_for_login_page(page)
        
        # 填写凭据
        self._fill_credentials(page)
        
        # 处理验证码
        self._fill_captcha(page)
        
        # 勾选记住我
        self._check_remember_me(page)
        
        # 提交表单
        self._submit_login(page)
        
        # 等待结果
        self._wait_for_redirect(page)
        return True

    def _acquire_vpn_cookies(self, context: BrowserContext, page: Page):
        """
        通过 authserver 登录 VPN CAS 回调，获取 VPN cookies
        
        :param context: 浏览器上下文
        :param page: 浏览器页面
        """
        self._log(LogLevel.INFO, "获取 VPN cookies...")
        
        vpn_login_url = f"{AUTHSERVER_NORMAL}/authserver/login?service={VPN_CAS_CALLBACK}"
        
        last_error = None
        for attempt in range(self.MAX_LOGIN_RETRIES):
            try:
                if attempt > 0:
                    self._log(LogLevel.INFO, f"重试获取 VPN cookies [{attempt + 1}/{self.MAX_LOGIN_RETRIES}]...")
                
                self._do_login_attempt(page, vpn_login_url)
                
                # 提取 VPN cookies
                all_cookies = context.cookies()
                self.vpn_cookies = self._extract_vpn_cookies(all_cookies)
                
                if self.vpn_cookies:
                    self._log(LogLevel.INFO, f"已获取 VPN cookies ({len(self.vpn_cookies)} 个)")
                    return
                else:
                    raise LoginError("登录成功但未获取到 VPN cookies")
                    
            except CaptchaError as e:
                last_error = e
                self._log(LogLevel.TRACE, f"验证码错误: {e}")
                continue
        
        raise CaptchaError(f"获取 VPN cookies 失败: {last_error}")

    def _login_with_vpn(self, context: BrowserContext, page: Page) -> dict:
        """
        VPN 模式登录流程
        
        :return: 最终的 cookies 字典
        """
        self._log(LogLevel.INFO, "VPN 模式登录...")
        
        # 尝试使用现有 VPN cookies
        if self.vpn_cookies:
            self._load_vpn_cookies(context)
            
            # 尝试访问 VPN 版 authserver
            vpn_login_url = f"{AUTHSERVER_VPN}/authserver/login?service={self.service}"
            self._log(LogLevel.TRACE, f"尝试使用现有 VPN cookies...")
            page.goto(vpn_login_url)
            
            # 检查 VPN cookies 是否有效
            if not self._is_vpn_login_redirect(page):
                self._log(LogLevel.INFO, "VPN cookies 有效")
                # cookies 有效，继续登录流程
                return self._complete_vpn_login(context, page, vpn_login_url)
            
            self._log(LogLevel.INFO, "VPN cookies 已失效，重新获取...")
        
        # 需要获取新的 VPN cookies
        self._acquire_vpn_cookies(context, page)
        
        # 重新加载 VPN cookies
        self._load_vpn_cookies(context)
        
        # 使用新 cookies 登录
        vpn_login_url = f"{AUTHSERVER_VPN}/authserver/login?service={self.service}"
        return self._complete_vpn_login(context, page, vpn_login_url)

    def _complete_vpn_login(self, context: BrowserContext, page: Page, login_url: str) -> dict:
        """
        完成 VPN 登录流程（已有有效 VPN cookies）
        
        :return: cookies 字典
        """
        last_error = None
        
        for attempt in range(self.MAX_LOGIN_RETRIES):
            try:
                if attempt > 0:
                    self._log(LogLevel.INFO, f"登录重试 [{attempt + 1}/{self.MAX_LOGIN_RETRIES}]...")
                
                # 访问目标 service 的登录页
                page.goto(login_url)
                
                # SSO 已登录则自动跳转
                if not self._is_on_login_page(page):
                    self._log(LogLevel.INFO, "登录成功（SSO 自动认证）")
                    break
                
                self._do_login_attempt(page, login_url)
                self._log(LogLevel.INFO, "登录成功")
                break
                
            except CaptchaError as e:
                last_error = e
                self._log(LogLevel.TRACE, f"验证码错误: {e}")
                continue
        else:
            raise CaptchaError(f"登录失败: {last_error}")
        
        # 返回所有 cookies
        all_cookies = context.cookies()
        return {item['name']: item['value'] for item in all_cookies}

    def _login_normal(self, context: BrowserContext, page: Page) -> dict:
        """
        普通模式登录流程
        
        :return: cookies 字典
        """
        self._log(LogLevel.INFO, "普通模式登录...")
        
        login_url = f"{AUTHSERVER_NORMAL}/authserver/login?service={self.service}"
        last_error = None
        
        for attempt in range(self.MAX_LOGIN_RETRIES):
            try:
                if attempt > 0:
                    self._log(LogLevel.INFO, f"登录重试 [{attempt + 1}/{self.MAX_LOGIN_RETRIES}]...")
                
                self._do_login_attempt(page, login_url)
                self._log(LogLevel.INFO, "登录成功")
                
                all_cookies = context.cookies()
                return {item['name']: item['value'] for item in all_cookies}
                
            except CaptchaError as e:
                last_error = e
                self._log(LogLevel.TRACE, f"验证码错误: {e}")
                continue
        
        raise CaptchaError(f"登录失败: {last_error}")

    def login(self) -> dict:
        """
        执行登录流程
        
        :return: 登录成功后的 cookies 字典
        :raises CredentialError: 用户名或密码错误
        :raises CaptchaError: 验证码多次失败
        :raises LoginError: 其他登录错误
        """
        with sync_playwright() as playwright:
            context = self._setup_browser_context(playwright)
            page = context.new_page()
            
            try:
                if self.use_vpn:
                    return self._login_with_vpn(context, page)
                else:
                    return self._login_normal(context, page)
            finally:
                context.browser.close()


# ==================== 命令行入口 ====================

if __name__ == "__main__":
    import sys
    import json
    
    if len(sys.argv) < 3:
        print("用法: python NuistLogin.py <学号> <密码> [--vpn] [--vpn-cookies vpn_cookies.json]")
        print("示例:")
        print("  python NuistLogin.py 202512345678 yourpassword")
        print("  python NuistLogin.py 202512345678 yourpassword --vpn")
        print("  python NuistLogin.py 202512345678 yourpassword --vpn --vpn-cookies vpn_cookies.json")
        sys.exit(1)
    
    user = sys.argv[1]
    pwd = sys.argv[2]
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
            password=pwd,
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