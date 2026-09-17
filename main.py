# -*- coding: utf-8 -*-
import requests
import json
import argparse
import os
import pickle
import truststore
from pathlib import Path
from urllib.parse import urljoin, urlsplit
truststore.inject_into_ssl()

# URL 配置
BASE_URL_NORMAL = "https://jwxt.nuist.edu.cn"
BASE_URL_VPN = "https://client.vpn.nuist.edu.cn/https/webvpn0852a5f822ad5ca19fb52006c843ea2e7397e76d41c77f8a91f1345208e4e34b"
VPN_COOKIES_FILE = Path(__file__).parent / "vpn_cookies.json"
VPN_DOMAIN = "client.vpn.nuist.edu.cn"
COMMON_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_AUTH_REDIRECTS = 10

# 全局变量，运行时设置
BASE_URL = BASE_URL_NORMAL
USE_VPN = False


class AuthenticationExpired(Exception):
    """业务请求已被重定向到认证流程。"""


def load_vpn_cookies():
    """
    从配置路径加载 vpn_cookies.json
    :return: cookies 字典，失败时返回 None
    """
    if not VPN_COOKIES_FILE.exists():
        print(f"[!] VPN cookies 文件不存在: {VPN_COOKIES_FILE}")
        return None
    try:
        with open(VPN_COOKIES_FILE, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
        print("[*] 已加载 VPN cookies")
        return cookies
    except (json.JSONDecodeError, IOError) as e:
        print(f"[!] 加载 VPN cookies 失败: {e}")
        return None


def save_vpn_cookies(cookies):
    """只保存 WebVPN 网关 Cookie，保持原有 {name: value} 格式。"""
    if not cookies:
        return False
    temp_file = VPN_COOKIES_FILE.with_suffix(f"{VPN_COOKIES_FILE.suffix}.tmp")
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, VPN_COOKIES_FILE)
        print("[*] VPN cookies 已更新")
        return True
    except (IOError, OSError) as e:
        print(f"[!] 保存 VPN cookies 失败: {e}")
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False


try:
    os.sys.path.append(str(Path(__file__).parent.parent))  # 确保当前目录在 sys.path 中
    from NuistLogin import NuistLogin
except ImportError:
    print("错误: 未找到 NuistLogin.py，请确保文件在同一目录下。")
    exit(1)


def create_session():
    """创建使用统一客户端标识的请求会话。"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': COMMON_USER_AGENT,
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    })
    return session


def install_vpn_gateway_cookies(session, cookies):
    """将 vpn_cookies.json 中的 Cookie 限定在 WebVPN 网关根路径。"""
    for name, value in (cookies or {}).items():
        session.cookies.set(
            name,
            value,
            domain=VPN_DOMAIN,
            path='/',
            secure=True,
        )


def save_cookies(cookies):
    """保存带完整 domain/path 作用域的 CookieJar。"""
    if not isinstance(cookies, requests.cookies.RequestsCookieJar):
        print("[!] 拒绝保存非 CookieJar 格式的 Cookies")
        return False
    if any(not cookie.domain or not cookie.path for cookie in cookies):
        print("[!] 拒绝保存缺少 domain/path 作用域的 Cookies")
        return False
    try:
        with open(COOKIES_CACHE_FILE, 'wb') as f:
            # 直接保存 cookiejar 对象，避免同名 cookie 冲突
            pickle.dump(cookies, f)
        print("[*] Cookies 已更新")
        return True
    except (IOError, pickle.PickleError) as e:
        print(f"[!] 保存 Cookies 失败: {e}")
        return False


def load_cookies():
    """从文件加载 cookies（返回 RequestsCookieJar）"""
    if not COOKIES_CACHE_FILE.exists():
        return None
    try:
        with open(COOKIES_CACHE_FILE, 'rb') as f:
            cookies = pickle.load(f)
        if not isinstance(cookies, requests.cookies.RequestsCookieJar):
            print("[*] Cookie 缓存格式过旧，将重新登录。")
            delete_cookies_cache()
            return None
        if any(not cookie.domain or not cookie.path for cookie in cookies):
            print("[*] Cookie 缓存缺少域名或路径作用域，将重新登录。")
            delete_cookies_cache()
            return None
        print("[*] 已从缓存加载 Cookies。")
        return cookies
    except (IOError, pickle.PickleError, EOFError, AttributeError, TypeError, ValueError) as e:
        print(f"[!] 加载 Cookies 失败: {e}")
        return None


def delete_cookies_cache():
    """删除 cookies 缓存文件"""
    try:
        if COOKIES_CACHE_FILE.exists():
            COOKIES_CACHE_FILE.unlink()
            print("[*] 已删除失效的 Cookies 缓存。")
    except IOError as e:
        print(f"[!] 删除 Cookies 缓存失败: {e}")


def sanitize_url(url):
    """移除可能包含 CAS ticket 的查询参数，仅用于日志。"""
    parsed = urlsplit(url)
    path = parsed.path or '/'
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def request_origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def allowed_auth_hosts():
    hosts = {urlsplit(BASE_URL).hostname}
    if not USE_VPN:
        hosts.add(urlsplit("https://authserver.nuist.edu.cn").hostname)
    return hosts


def is_expected_service_url(url):
    parsed = urlsplit(url)
    base = urlsplit(BASE_URL)
    service_prefix = f"{base.path.rstrip('/')}/jwapp/"
    return parsed.hostname == base.hostname and parsed.path.startswith(service_prefix)


def is_login_response(response):
    url = response.url.lower()
    if '/authserver/login' in url or '/enlink/sso/login' in url:
        return True
    body = response.text
    return 'id="pwdFromId"' in body or "id='pwdFromId'" in body


def format_redirect_chain(chain):
    return ' -> '.join(f"{status} {url}" for status, url in chain)


def get_with_controlled_redirects(session, url, headers=None, timeout=10):
    """在限定域名和跳数内跟随认证重定向，并返回脱敏链路。"""
    current_url = url
    visited_redirects = set()
    chain = []
    allowed_hosts = allowed_auth_hosts()

    for redirect_count in range(MAX_AUTH_REDIRECTS + 1):
        if urlsplit(current_url).hostname not in allowed_hosts:
            return None, chain, f"重定向到了非预期域名: {urlsplit(current_url).hostname}"

        response = session.get(
            current_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        chain.append((response.status_code, sanitize_url(response.url)))

        if response.status_code not in REDIRECT_STATUS_CODES:
            return response, chain, None

        location = response.headers.get('Location')
        if not location:
            return None, chain, "重定向响应缺少 Location"
        if redirect_count >= MAX_AUTH_REDIRECTS:
            return None, chain, f"认证重定向超过 {MAX_AUTH_REDIRECTS} 次"
        next_url = urljoin(response.url, location)
        redirect_edge = (current_url, next_url)
        if redirect_edge in visited_redirects:
            return None, chain, "检测到重复重定向路径"
        visited_redirects.add(redirect_edge)
        current_url = next_url

    return None, chain, f"认证重定向超过 {MAX_AUTH_REDIRECTS} 次"


def check_cookies_valid(session, cookies, user=""):
    """
    检查 cookies 是否仍然有效
    通过访问教务系统页面，检查是否会重定向到 authserver.nuist.edu.cn
    如果 authserver cookies 有效，会自动完成认证并更新 session cookies
    :param session: requests.Session 对象（会被更新 cookies）
    :param cookies: RequestsCookieJar（保留 domain/path 作用域）
    :return: True 如果有效，False 如果失效，None 如果因网络错误无法判断
    """
    if not cookies:
        return False
    
    test_url = f"{BASE_URL}/jwapp/sys/emaphome/portal/index.do"
    
    try:
        session.cookies.update(cookies)
        response, chain, redirect_error = get_with_controlled_redirects(session, test_url)
        if redirect_error:
            print(f"[!] Cookie 检查失败: {redirect_error}")
            if chain:
                print(f"[!] 重定向链: {format_redirect_chain(chain)}")
            return False
        if response.status_code != 200 or not is_expected_service_url(response.url):
            print(f"[*] Cookies 已失效，最终响应: {response.status_code} {sanitize_url(response.url)}")
            return False
        if is_login_response(response):
            print("[*] Cookies 已失效，需要重新登录。")
            return False
        
        print(f"[*] {user} Cookies 有效")
        return True

    except requests.exceptions.RequestException as e:
        print(f"[!] 检查 Cookies 有效性时发生错误: {e}")
        return None

def parse_grades_data(json_data):
    """
    解析接口返回的JSON数据，提取关键成绩信息
    :param json_data: 接口返回的字典对象
    :return: 解析后的成绩列表
    """
    parsed_list = []
    
    # 防御性编程：检查数据层级是否存在
    try:
        rows = json_data.get("datas", {}).get("xscjcx", {}).get("rows", [])
    except AttributeError:
        print("解析错误: 返回的数据结构不符合预期")
        return []

    for item in rows:
        # 提取字段，根据提供的JSON样本映射
        course_info = {
            "学期": item.get("XNXQDM_DISPLAY"),      # e.g. "2025-2026-1学期"
            "课程名称": item.get("KCM"),             # e.g. "心理健康教育"
            "课程代码": item.get("KCH"),             # e.g. "25000195"
            "成绩": item.get("XSZCJMC"),             # e.g. "94" (显示成绩，兼容等级制)
            "原始分数": item.get("ZCJ"),             # e.g. 94.0 (数值)
            "学分": item.get("XF"),                  # e.g. 2.0
            "绩点": item.get("XFJD"),                # e.g. 4.4
            "课程性质": item.get("KCXZDM_DISPLAY"),  # e.g. "通修(必)"
            "考核方式": item.get("KSLXDM_DISPLAY"),  # e.g. "考试"
            "是否通过": item.get("SFJG_DISPLAY")     # e.g. "是"
        }
        parsed_list.append(course_info)
        
    return parsed_list

def load_cached_grades():
    """从缓存文件加载已保存的成绩"""
    if GRADES_CACHE_FILE.exists():
        try:
            with open(GRADES_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []

def save_grades_cache(grades):
    """保存成绩到缓存文件"""
    with open(GRADES_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(grades, f, ensure_ascii=False, indent=2)

def find_new_grades(current_grades, cached_grades):
    """
    对比当前成绩和缓存成绩，找出新增的成绩
    使用课程代码作为唯一标识
    """
    cached_codes = {g['课程代码'] for g in cached_grades}
    new_grades = [g for g in current_grades if g['课程代码'] not in cached_codes]
    return new_grades

def fetch_gpa(session):
    """
    获取最新的 GPA
    :param session: 已登录的 requests.Session 对象
    :return: GPA 字符串，失败时返回 None
    """
    gpa_url = f'{BASE_URL}/jwapp/sys/cjcx/modules/cjfx/cxxsgpa.do'
    
    headers = {
        'User-Agent': COMMON_USER_AGENT,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': request_origin(BASE_URL),
        'Referer': f'{BASE_URL}/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh',
    }
    
    try:
        response = session.post(
            gpa_url,
            headers=headers,
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_STATUS_CODES or response.status_code in (401, 403):
            raise AuthenticationExpired(
                f"GPA 请求需要重新认证: {response.status_code} {sanitize_url(response.url)}"
            )
        response.raise_for_status()
        if is_login_response(response):
            raise AuthenticationExpired("GPA 请求返回了登录页面")
        json_data = response.json()
        
        if json_data.get("code") == "0" or json_data.get("code") == 0:
            rows = json_data.get("datas", {}).get("cxxsgpa", {}).get("rows", [])
            if rows and len(rows) > 0:
                gpa = rows[0].get("GPA")
                print(f"[*] 获取到 GPA: {gpa}")
                return gpa
        print("[!] 获取 GPA 失败: 数据格式异常")
        return None
    except json.JSONDecodeError:
        print("[!] GPA 返回内容不是有效的 JSON")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[!] 获取 GPA 时发生错误: {e}")
        return None

def send_onebot_notification(user, qq_number, new_grades, gpa=None, webhook_url="http://127.0.0.1:3000/send_private_msg"):
    """
    通过 OneBot HTTP API 发送私聊通知
    :param user: 学号（用于多用户区分）
    :param qq_number: 目标QQ号
    :param new_grades: 新增的成绩列表
    :param gpa: 最新的 GPA
    :param webhook_url: OneBot HTTP API 地址
    :return: 是否发送成功
    """
    # 将 args.multi 规范化为布尔值，避免字符串 'False' 被当作真值
    multi_raw = getattr(args, "multi", False)
    if isinstance(multi_raw, str):
        multi_enabled = multi_raw.strip().lower() in ("1", "true", "yes", "y", "on")
    else:
        multi_enabled = bool(multi_raw)

    # 构建消息内容
    if not multi_enabled:
        if len(new_grades) == 1:
            grade = new_grades[0]
            message_lines = [f"📢 {grade['课程名称']} 成绩更新通知"]
        else:
            message_lines = [f"📢 成绩更新通知"]
    else:
        message_lines = [f"📢 {user} 成绩更新通知"]
    for grade in new_grades:
        message_lines.append(
            f"📚 {grade['课程名称']}\n"
            f"   成绩: {grade['成绩']} | 学分: {grade['学分']} | 绩点: {grade['绩点']}\n"
            f"   学期: {grade['学期']}"
        )
    message = "\n\n".join(message_lines)
    
    # 添加 GPA 到消息末尾
    if gpa:
        message += f"\n\n📊 当前 GPA: {gpa}"
    
    payload = {
        "user_id": int(qq_number),
        "message": message
    }
    
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"[!] 发送通知失败: HTTP {response.status_code}\n请求body: {payload}\n响应body: {response.text}")
            return False
        resp_data = response.json()
        if resp_data.get("status") != "ok":
            print(f"[!] 发送通知失败\n请求body: {payload}\n响应body: {resp_data}")
            return False
        print(f"[*] 通知已发送到 QQ: {qq_number}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"[!] 发送通知时发生错误: {e}\n请求body: {payload}")
        return False

def fetch_grades(user, pwd):
    """
    登录并获取成绩原始数据
    优先使用缓存的 cookies，失效时重新登录
    从检查 cookies 开始到脚本结束使用同一个 session
    VPN 模式下需要同时使用 vpn_cookies 和 jwxt cookies
    :return: (包含成绩信息的列表, session 对象)
    """
    session = create_session()
    need_login = True
    vpn_cookies = None
    
    # VPN 模式：先加载 vpn_cookies
    if USE_VPN:
        vpn_cookies = load_vpn_cookies()
        if not vpn_cookies:
            print("[!] VPN 模式下未能加载 vpn_cookies，无法继续")
            return [], None
        install_vpn_gateway_cookies(session, vpn_cookies)
        print("[*] VPN 模式: 已加载 WebVPN cookies")
    
    # 尝试使用缓存的 jwxt cookies
    cached_cookies = load_cookies()
    if cached_cookies:
        # 使用同一个 session 检查 cookies 有效性
        # 如果 authserver cookies 有效，会自动完成认证
        cookies_valid = check_cookies_valid(session, cached_cookies, user)
        if cookies_valid is True:
            need_login = False
        elif cookies_valid is False:
            # 缓存的 cookies 完全失效，需要重新登录
            delete_cookies_cache()
            # 重置 session 以便重新登录
            session = create_session()
        else:
            # 网络错误无法判断缓存是否失效，保留缓存供下次使用
            return [], None
    
    if need_login:
        # service 始终使用原始 URL
        login_url = "https://jwxt.nuist.edu.cn/jwapp/sys/emaphome/portal/index.do"
        print(f"[*] 正在登录用户: {user} ...{'(VPN模式)' if USE_VPN else ''}")
        
        try:
            bot = NuistLogin(user, pwd, login_url, headless=True,
                             use_vpn=USE_VPN, vpn_cookies=vpn_cookies,
                             user_agent=COMMON_USER_AGENT)
            # 新版 NuistLogin 直接返回 RequestsCookieJar，保留 domain/path 作用域
            cookies = bot.login(cookie_format="jar")
            if USE_VPN and bot.vpn_cookies and bot.vpn_cookies != vpn_cookies:
                save_vpn_cookies(bot.vpn_cookies)
        except Exception as e:
            print(f"[!] 登录过程发生错误: {e}")
            return [], None
        
        if not cookies:
            print("[!] 登录失败，无法获取 Cookies")
            return [], None

        print("[*] 登录成功！")
        # 登录前的旧 Session 不再复用，避免失效 Cookie 混入新会话。
        session = create_session()
        session.cookies.update(cookies)
    
    print("[*] 正在获取成绩...")

    # 3. 准备请求数据
    target_url = f'{BASE_URL}/jwapp/sys/cjcx/modules/cjcx/xscjcx.do'
    
    # Headers
    headers = {
        'User-Agent': COMMON_USER_AGENT,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': request_origin(BASE_URL),
        'Referer': f'{BASE_URL}/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
    }

    # Form Data Payload
    # 注意：querySetting 是一个 JSON 字符串
    payload = {
        'querySetting': json.dumps([
            {"name": "SFYX", "caption": "是否有效", "linkOpt": "AND", "builderList": "cbl_m_List", "builder": "m_value_equal", "value": "1", "value_display": "是"},
            {"name": "*order", "value": "-KSSJ,-XNXQDM", "linkOpt": "AND", "builder": "m_value_equal"}
        ]),
        '*order': '-KSSJ,-XNXQDM',
        'pageSize': '50',
        'pageNumber': '1'
    }

    # 4. 发送请求
    try:
        index_url = f"{BASE_URL}/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh"
        index_response, chain, redirect_error = get_with_controlled_redirects(
            session,
            index_url,
            timeout=10,
        )
        if redirect_error:
            print(f"[!] 成绩页面认证失败: {redirect_error}")
            if chain:
                print(f"[!] 重定向链: {format_redirect_chain(chain)}")
            delete_cookies_cache()
            return [], None
        if index_response.status_code != 200 or not is_expected_service_url(index_response.url):
            print(
                f"[!] 成绩页面认证失败: {index_response.status_code} "
                f"{sanitize_url(index_response.url)}"
            )
            delete_cookies_cache()
            return [], None

        response = session.post(
            target_url,
            headers=headers,
            data=payload,
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_STATUS_CODES or response.status_code in (401, 403):
            print(
                f"[!] 成绩请求需要重新认证: {response.status_code} "
                f"{sanitize_url(response.url)}"
            )
            delete_cookies_cache()
            return [], None
        response.raise_for_status()
        if is_login_response(response):
            print("[!] 成绩请求返回了登录页面，需要重新认证。")
            delete_cookies_cache()
            return [], None
        
        json_data = response.json()
        
        # 检查返回数据是否正常
        if json_data.get("code") != "0" and json_data.get("code") != 0:
            error_msg = json_data.get("msg", "未知错误")
            print(f"[!] 接口返回错误: {error_msg}")
            # 如果是认证相关错误，删除 cookies 缓存
            delete_cookies_cache()
            return [], None
        
        # 解析数据
        return parse_grades_data(json_data), session

    except json.JSONDecodeError:
        print("[!] 返回内容不是有效的 JSON")
        delete_cookies_cache()
        print(response.text[:500])  # 调试用，只打印前500字符
        return [], None
    except requests.exceptions.RequestException as e:
        print(f"[!] 网络请求错误: {e}")
        return [], None

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='NUIST 成绩查询工具')
    parser.add_argument('-u', '--user', required=True, help='登录用户名')
    parser.add_argument('-p', '--password', required=True, help='登录密码')
    parser.add_argument('-qq', '--qq', required=True, help='接收通知的QQ号')
    parser.add_argument('--webhook', default='http://127.0.0.1:3000/send_private_msg', 
                        help='OneBot HTTP API 地址 (默认: http://127.0.0.1:3000/send_private_msg)')
    parser.add_argument('--multi', action='store_true', required=False, default=False, help='多用户模式：在通知中显示学号')
    parser.add_argument('--vpn', action='store_true', required=False, default=False, 
                        help='使用 WebVPN 访问教务系统')
    parser.add_argument('--vpn-cookies', dest='vpn_cookies_path', default=None,
                        help='VPN cookies 文件路径（相对或绝对路径，默认: 同级目录下的 vpn_cookies.json）')
    return parser.parse_args()

if __name__ == "__main__":
    # 解析命令行参数
    args = parse_args()
    
    # 设置 VPN cookies 文件路径
    if args.vpn_cookies_path:
        # 支持相对路径和绝对路径
        vpn_cookies_path = Path(args.vpn_cookies_path)
        if not vpn_cookies_path.is_absolute():
            vpn_cookies_path = Path.cwd() / vpn_cookies_path
        VPN_COOKIES_FILE = vpn_cookies_path.resolve()
    # 否则使用默认的同级目录
    
    # 根据 --vpn 参数设置 BASE_URL
    if args.vpn:
        USE_VPN = True
        BASE_URL = BASE_URL_VPN
        print(f"[*] 使用 WebVPN 模式")
    else:
        USE_VPN = False
        BASE_URL = BASE_URL_NORMAL
    
    # 成绩缓存文件和 Cookies 缓存文件路径（VPN 模式使用独立缓存）
    vpn_suffix = "_vpn" if USE_VPN else ""
    GRADES_CACHE_FILE = Path(__file__).parent / f"grades_cache_{args.user}.json"
    COOKIES_CACHE_FILE = Path(__file__).parent / f"cookies_cache_{args.user}{vpn_suffix}.pkl"
    
    # 执行获取成绩，并在所有业务请求结束后统一保存 session 中的 cookies
    session = None
    should_save_cookies = False
    try:
        grade_list, session = fetch_grades(args.user, args.password)
        should_save_cookies = session is not None

        if not grade_list:
            print("[*] 未获取到成绩或列表为空。")
            exit(0)

        # 加载缓存的成绩
        cached_grades = load_cached_grades()

        # 查找新增的成绩
        new_grades = find_new_grades(grade_list, cached_grades)

        if new_grades:
            # 有新成绩，获取最新 GPA
            gpa = fetch_gpa(session) if session else None

            # 输出并发送通知
            print(f"\n[*] 发现 {len(new_grades)} 门新成绩:\n")
            print("-" * 60)
            print(f"{'课程名称':<20} | {'成绩':<5} | {'学分':<5} | {'学期'}")
            print("-" * 60)
            for course in new_grades:
                print(f"{course['课程名称']:<20} | {course['成绩']:<5} | {course['学分']:<5} | {course['学期']}")

            # 发送 OneBot 通知
            if send_onebot_notification(args.user, args.qq, new_grades, gpa, args.webhook):
                # 只有通知发送成功才保存缓存
                save_grades_cache(grade_list)
                print("[*] 成绩已记录到缓存。")
            else:
                print("[!] 通知发送失败，成绩未记录到缓存。")
        else:
            # 没有新成绩，也更新缓存（保持数据同步）
            save_grades_cache(grade_list)
            print("[*] 没有新成绩。")
    except AuthenticationExpired as e:
        should_save_cookies = False
        print(f"[!] {e}")
        delete_cookies_cache()
    except requests.exceptions.TooManyRedirects as e:
        should_save_cookies = False
        print(f"[!] 请求重定向次数超过限制，Cookies 已失效: {e}")
        delete_cookies_cache()
    finally:
        if should_save_cookies:
            save_cookies(session.cookies)
