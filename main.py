# -*- coding: utf-8 -*-
import requests
import json
import argparse
import os
import pickle
import truststore
from pathlib import Path
truststore.inject_into_ssl()

# 假设 NuistLogin.py 在同级目录下
try:
    from NuistLogin import NuistLogin
except ImportError:
    print("错误: 未找到 NuistLogin.py，请确保文件在同一目录下。")
    exit(1)

def save_cookies(cookies):
    """保存 cookies 到文件（支持 cookiejar 或 dict）"""
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
    """从文件加载 cookies（返回 cookiejar 或 dict）"""
    if not COOKIES_CACHE_FILE.exists():
        return None
    try:
        with open(COOKIES_CACHE_FILE, 'rb') as f:
            cookies = pickle.load(f)
        print("[*] 已从缓存加载 Cookies。")
        return cookies
    except (IOError, pickle.PickleError, EOFError) as e:
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


def check_cookies_valid(session, cookies, user=""):
    """
    检查 cookies 是否仍然有效
    通过访问教务系统页面，检查是否会重定向到 authserver.nuist.edu.cn
    如果 authserver cookies 有效，会自动完成认证并更新 session cookies
    :param session: requests.Session 对象（会被更新 cookies）
    :param cookies: cookies 字典
    :return: True 如果有效（包括自动认证成功），False 如果失效
    """
    if not cookies:
        return False
    
    test_url = "https://jwxt.nuist.edu.cn/jwapp/sys/emaphome/portal/index.do"
    
    try:
        session.cookies.update(cookies)

        # 允许重定向检查是否有效（authserver cookies 有效时会自动认证并跳回）
        response = session.get(test_url, timeout=10, allow_redirects=True)
        
        # 检查是否发生了重定向（history 非空表示有跳转）
        if response.history:
            # print(f"[*] 检测到 {len(response.history)} 次重定向")

            if 'authserver.nuist.edu.cn' in response.url or 'login' in response.url or response.status_code != 200:
                print("[*] Cookies 已失效，需要重新登录。")
                return False
            else:
                save_cookies(session.cookies)
        
        # 检查响应内容中是否包含登录页面特征
        if '统一身份认证' in response.text or 'authserver' in response.text:
            print("[*] Cookies 已失效，需要重新登录。")
            return False
        
        print(f"[*] {user} Cookies 有效")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"[!] 检查 Cookies 有效性时发生错误: {e}")
        return False

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
    gpa_url = 'https://jwxt.nuist.edu.cn/jwapp/sys/cjcx/modules/cjfx/cxxsgpa.do'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://jwxt.nuist.edu.cn',
        'Referer': 'https://jwxt.nuist.edu.cn/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh',
    }
    
    try:
        response = session.post(gpa_url, headers=headers, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        
        if json_data.get("code") == "0" or json_data.get("code") == 0:
            rows = json_data.get("datas", {}).get("cxxsgpa", {}).get("rows", [])
            if rows and len(rows) > 0:
                gpa = rows[0].get("GPA")
                print(f"[*] 获取到 GPA: {gpa}")
                return gpa
        print("[!] 获取 GPA 失败: 数据格式异常")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[!] 获取 GPA 时发生错误: {e}")
        return None
    except json.JSONDecodeError:
        print("[!] GPA 返回内容不是有效的 JSON")
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
    # 构建消息内容
    if not args.multi:
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
        if response.status_code == 200:
            print(f"[*] 通知已发送到 QQ: {qq_number}")
            return True
        else:
            print(f"[!] 发送通知失败: HTTP {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[!] 发送通知时发生错误: {e}")
        return False

def fetch_grades(user, pwd):
    """
    登录并获取成绩原始数据
    优先使用缓存的 cookies，失效时重新登录
    从检查 cookies 开始到脚本结束使用同一个 session
    :return: (包含成绩信息的列表, session 对象)
    """
    session = requests.Session()
    need_login = True
    
    # 1. 尝试使用缓存的 cookies
    cached_cookies = load_cookies()
    if cached_cookies:
        # 使用同一个 session 检查 cookies 有效性
        # 如果 authserver cookies 有效，会自动完成认证
        if check_cookies_valid(session, cached_cookies, user):
            need_login = False
        else:
            # 缓存的 cookies 完全失效，需要重新登录
            delete_cookies_cache()
            # 重置 session 以便重新登录
            session = requests.Session()
    
    if need_login:
        login_url = "https://jwxt.nuist.edu.cn/jwapp/sys/emaphome/portal/index.do"
        print(f"[*] 正在登录用户: {user} ...")
        
        try:
            bot = NuistLogin(user, pwd, login_url, headless=True)
            cookies = bot.login()  # 获取登录后的 cookies
        except Exception as e:
            print(f"[!] 登录过程发生错误: {e}")
            return [], None
        
        if not cookies:
            print("[!] 登录失败，无法获取 Cookies")
            return [], None

        print("[*] 登录成功！")
        # 保存新的 cookies
        save_cookies(cookies)
        # 更新 session cookies
        session.cookies.update(cookies)
    
    print("[*] 正在获取成绩...")

    # 3. 准备请求数据
    target_url = 'https://jwxt.nuist.edu.cn/jwapp/sys/cjcx/modules/cjcx/xscjcx.do'
    
    # Headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://jwxt.nuist.edu.cn',
        'Referer': 'https://jwxt.nuist.edu.cn/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh',
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
        # 使用 session 发送请求
        session.get("https://jwxt.nuist.edu.cn/jwapp/sys/cjcx/*default/index.do?EMAP_LANG=zh#/cjcx")  # 建立session
        response = session.post(target_url, headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        
        # 检查是否被重定向到登录页面（cookies 可能在请求过程中失效）
        if 'authserver.nuist.edu.cn' in response.url or response.status_code != 200:
            print("[!] 请求过程中 Cookies 失效，请重新运行。")
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

    except requests.exceptions.RequestException as e:
        print(f"[!] 网络请求错误: {e}")
        delete_cookies_cache()
        return [], None
    except json.JSONDecodeError:
        print("[!] 返回内容不是有效的 JSON")
        delete_cookies_cache()
        print(response.text[:500])  # 调试用，只打印前500字符
        return [], None

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='NUIST 成绩查询工具')
    parser.add_argument('-u', '--user', required=True, help='登录用户名')
    parser.add_argument('-p', '--password', required=True, help='登录密码')
    parser.add_argument('-qq', '--qq', required=True, help='接收通知的QQ号')
    parser.add_argument('--webhook', default='http://127.0.0.1:3000/send_private_msg', 
                        help='OneBot HTTP API 地址 (默认: http://127.0.0.1:3000/send_private_msg)')
    parser.add_argument('--multi', required=False, default=False, help='通知学号')
    return parser.parse_args()

if __name__ == "__main__":
    # 解析命令行参数
    args = parse_args()
    
    # 成绩缓存文件和 Cookies 缓存文件路径
    GRADES_CACHE_FILE = Path(__file__).parent / f"grades_cache_{args.user}.json"
    COOKIES_CACHE_FILE = Path(__file__).parent / f"cookies_cache_{args.user}.pkl"
    
    # 执行获取成绩
    grade_list, session = fetch_grades(args.user, args.password)
    
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