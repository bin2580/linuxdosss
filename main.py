"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup

# 兼容通用的 notify 模块（修复 send_all 调用失败问题）
try:
    from notify import NotificationManager
except ModuleNotFoundError:
    # 备用通知类（无 notify 模块时不报错）
    class NotificationManager:
        def __init__(self):
            pass
        def send(self, title, content, type="text"):
            logger.info(f"[通知] {title}: {content}")
        send_all = send  # 兼容原代码的 send_all 调用


def retry_decorator(retries=3, min_delay=3, max_delay=8):
    """通用重试装饰器（增加随机延迟，适配429限流）"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    # 每次请求前随机延迟（避免固定频率）
                    pre_delay = random.uniform(1, 3)
                    logger.info(f"执行 {func.__name__} 前延迟 {pre_delay:.2f}s")
                    time.sleep(pre_delay)
                    return func(*args, **kwargs)
                except Exception as e:
                    is_last_attempt = attempt == retries - 1
                    if is_last_attempt:
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                        raise e  # 最后一次失败抛出异常
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    # 指数退避延迟
                    sleep_s = random.uniform(min_delay * (attempt + 1), max_delay * (attempt + 1))
                    logger.info(f"将在 {sleep_s:.2f}s 后重试")
                    time.sleep(sleep_s)
            return None
        return wrapper
    return decorator


# 环境变量清理（适配Linux无桌面环境）
os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

# 配置读取（增加容错）
USERNAME = os.environ.get("LINUXDO_USERNAME") or os.environ.get("USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD") or os.environ.get("PASSWORD")
COOKIES = os.environ.get("LINUXDO_COOKIES", "").strip()
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in ["false", "0", "off"]

# 核心URL配置
HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"


class LinuxDoBrowser:
    def __init__(self) -> None:
        # 初始化浏览器配置（增强反爬，适配DrissionPage API）
        self._init_browser()
        # 初始化请求会话（带反爬头+重试）
        self._init_session()
        # 初始化通知管理器
        self.notifier = NotificationManager()

    def _init_browser(self):
        """初始化浏览器（适配DrissionPage的正确API）"""
        from sys import platform

        # 适配不同系统的User-Agent
        platform_map = {
            "linux": "X11; Linux x86_64",
            "linux2": "X11; Linux x86_64",
            "darwin": "Macintosh; Intel Mac OS X 10_15_7",
            "win32": "Windows NT 10.0; Win64; x64"
        }
        platformIdentifier = platform_map.get(platform, "X11; Linux x86_64")

        co = ChromiumOptions()
        # 基础配置
        co.headless(True).incognito(True).set_argument("--no-sandbox")
        # 反爬关键配置（DrissionPage 正确写法）
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--disable-extensions")
        # DrissionPage 设置实验性选项的正确方法（替换 Selenium 的 experimental_option）
        co.set_experimental_option("excludeSwitches", ["enable-automation"])
        co.set_experimental_option("useAutomationExtension", False)
        # 随机User-Agent
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.choice(['130.0.0.0', '142.0.0.0', '148.0.0.0'])} Safari/537.36"
        )

        self.browser = Chromium(co)
        self.page = self.browser.new_tab()

    def _init_session(self):
        """初始化请求会话（带反爬头+限流容错）"""
        self.session = requests.Session()
        # 随机User-Agent
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ]
        self.session.headers.update({
            "User-Agent": random.choice(user_agents),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": HOME_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> list[dict]:
        """解析Cookie字符串为DrissionPage格式"""
        cookies = []
        for part in cookie_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".linux.do",
                    "path": "/",
                })
        return cookies

    def login_with_cookies(self, cookie_str: str) -> bool:
        """Cookie登录（增加延迟+容错）"""
        logger.info("检测到手动Cookie，尝试Cookie登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie解析失败或为空")
            return False

        logger.info(f"成功解析 {len(dp_cookies)} 个Cookie条目")
        # 同步Cookie到requests会话
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain="linux.do")
        # 同步到浏览器
        self.page.set.cookies(dp_cookies)
        
        # 延迟后访问主页（避免触发限流）
        time.sleep(random.uniform(3, 5))
        self.page.get(HOME_URL)
        time.sleep(random.uniform(4, 6))

        # 验证登录状态（增强容错）
        try:
            user_ele = self.page.ele("@id=current-user", timeout=10)
            if user_ele:
                logger.info("Cookie登录验证成功")
                return True
        except Exception as e:
            logger.warning(f"Cookie登录验证异常: {str(e)}")
        
        # 备用验证方式
        if "avatar" in self.page.html.lower() or "logout" in self.page.html.lower():
            logger.info("Cookie登录验证成功（备用检测）")
            return True
        else:
            logger.error("Cookie登录验证失败，Cookie可能已过期")
            return False

    @retry_decorator(retries=3, min_delay=5, max_delay=10)
    def _get_csrf_token(self):
        """获取CSRF Token（带重试+429处理）"""
        logger.info("开始获取CSRF Token...")
        try:
            resp_csrf = self.session.get(
                CSRF_URL,
                impersonate=random.choice(["firefox135", "chrome136", "safari17"]),
                timeout=15
            )
            # 处理429/503等限流状态码
            if resp_csrf.status_code in [429, 503, 500]:
                raise Exception(f"CSRF获取限流，状态码: {resp_csrf.status_code}")
            if resp_csrf.status_code != 200:
                raise Exception(f"CSRF获取失败，状态码: {resp_csrf.status_code}")
            
            csrf_data = resp_csrf.json()
            csrf_token = csrf_data.get("csrf")
            if not csrf_token:
                raise Exception("CSRF Token为空")
            
            logger.info(f"CSRF Token获取成功: {csrf_token[:10]}...")
            return csrf_token
        except Exception as e:
            logger.error(f"获取CSRF Token失败: {str(e)}")
            raise e

    @retry_decorator(retries=2, min_delay=5, max_delay=8)
    def login(self):
        """账号密码登录（带重试+限流处理）"""
        logger.info("开始账号密码登录流程")
        # Step 1: 获取CSRF Token
        csrf_token = self._get_csrf_token()
        if not csrf_token:
            return False

        # Step 2: 构造登录请求
        headers = self.session.headers.copy()
        headers.update({
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://linux.do",
        })

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }

        # Step 3: 发送登录请求（随机模拟浏览器）
        logger.info("发送登录请求...")
        resp_login = self.session.post(
            SESSION_URL,
            data=data,
            impersonate=random.choice(["chrome136", "firefox135", "safari17"]),
            headers=headers,
            timeout=20
        )

        # Step 4: 处理登录响应
        if resp_login.status_code == 200:
            response_json = resp_login.json()
            if response_json.get("error"):
                raise Exception(f"登录失败: {response_json.get('error')}")
            logger.info("账号密码登录成功!")
        else:
            raise Exception(f"登录失败，状态码: {resp_login.status_code}, 响应: {resp_login.text[:200]}")

        # Step 5: 同步Cookie到浏览器
        logger.info("同步登录Cookie到浏览器...")
        cookies_dict = self.session.cookies.get_dict()
        dp_cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".linux.do",
                "path": "/",
            }
            for name, value in cookies_dict.items()
        ]
        self.page.set.cookies(dp_cookies)

        # 验证登录状态
        self.page.get(HOME_URL)
        time.sleep(random.uniform(4, 6))
        try:
            user_ele = self.page.ele("@id=current-user", timeout=10)
            if user_ele or "avatar" in self.page.html.lower():
                logger.info("登录状态验证成功")
                return True
            else:
                raise Exception("未找到用户标识，登录验证失败")
        except Exception as e:
            logger.error(f"登录验证失败: {str(e)}")
            return False

    def click_topic(self):
        """浏览主题帖（控制频率，避免429）"""
        try:
            topic_list = self.page.ele("@id=list-area").eles(".:title", timeout=10)
        except Exception as e:
            logger.error(f"获取主题帖列表失败: {str(e)}")
            return False
        
        if not topic_list:
            logger.error("未找到主题帖")
            return False
        
        # 随机选择5-8个（减少请求数，避免限流）
        select_count = random.randint(5, 8)
        logger.info(f"发现 {len(topic_list)} 个主题帖，随机选择 {select_count} 个浏览")
        
        for idx, topic in enumerate(random.sample(topic_list, select_count)):
            # 每浏览2个帖子增加一次长延迟
            if idx % 2 == 0 and idx > 0:
                long_delay = random.uniform(8, 12)
                logger.info(f"已浏览{idx}个帖子，延迟{long_delay:.2f}s避免限流")
                time.sleep(long_delay)
            self.click_one_topic(topic.attr("href"))
        
        return True

    @retry_decorator(retries=2, min_delay=3, max_delay=6)
    def click_one_topic(self, topic_url):
        """浏览单个帖子（增加随机行为）"""
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)
            time.sleep(random.uniform(3, 5))
            
            # 随机点赞（降低点赞频率）
            if random.random() < 0.2:
                self.click_like(new_page)
            
            # 随机滚动浏览
            self.browse_post(new_page)
            logger.info(f"成功浏览帖子: {topic_url[:50]}...")
        finally:
            try:
                new_page.close()
            except Exception as e:
                logger.warning(f"关闭帖子标签页失败: {str(e)}")

    def browse_post(self, page):
        """滚动浏览帖子（模拟真人行为）"""
        prev_url = None
        scroll_count = random.randint(5, 8)  # 减少滚动次数，避免限流
        logger.info(f"开始滚动浏览，预计滚动{scroll_count}次")
        
        for _ in range(scroll_count):
            # 随机滚动距离
            scroll_distance = random.randint(400, 600)
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"向下滚动 {scroll_distance} 像素")
            
            # 随机停留时间
            wait_time = random.uniform(2, 4)
            time.sleep(wait_time)
            
            # 随机退出（模拟真人不看完）
            if random.random() < 0.15:
                logger.info("随机退出帖子浏览")
                break
            
            # 检查是否到底
            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight - 200"
            )
            current_url = page.url
            if at_bottom or current_url == prev_url:
                logger.info("到达页面底部或无新内容，退出浏览")
                break
            prev_url = current_url

    def click_like(self, page):
        """点赞操作（增强容错）"""
        try:
            # 精准定位未点赞的按钮
            like_buttons = page.eles(".discourse-reactions-reaction-button:not(.reacted)", timeout=5)
            if like_buttons:
                like_btn = like_buttons[0]
                like_btn.click()
                time.sleep(random.uniform(1, 2))
                logger.info("帖子点赞成功")
            else:
                logger.info("未找到可点赞的按钮（已点赞或无点赞功能）")
        except Exception as e:
            logger.warning(f"点赞操作失败: {str(e)}")

    @retry_decorator(retries=2, min_delay=3, max_delay=6)
    def print_connect_info(self):
        """获取并打印连接信息（带重试）"""
        logger.info("获取连接信息...")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": self.session.headers["User-Agent"],
        }
        resp = self.session.get(
            "https://connect.linux.do/",
            headers=headers,
            impersonate=random.choice(["chrome136", "safari17"]),
            timeout=15
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        info = []

        for row in rows:
            cells = row.select("td")
            if len(cells) >= 3:
                project = cells[0].text.strip()
                current = cells[1].text.strip() or "0"
                requirement = cells[2].text.strip() or "0"
                info.append([project, current, requirement])

        logger.info("--------------Connect Info-----------------")
        logger.info("\n" + tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))
        return info

    def send_notifications(self, browse_enabled):
        """发送通知（兼容通用notify模块）"""
        try:
            # 构造通知内容
            base_msg = f"✅ Linux.do 签到成功\n账号: {USERNAME}"
            if browse_enabled:
                base_msg += "\n📖 浏览任务已完成"
            
            # 获取连接信息补充到通知
            try:
                connect_info = self.print_connect_info()
                if connect_info:
                    base_msg += "\n\n📊 连接信息:\n" + tabulate(connect_info, headers=["项目", "当前", "要求"], tablefmt="simple")
            except Exception as e:
                logger.warning(f"获取连接信息失败，通知中跳过: {str(e)}")
            
            # 发送通知（兼容 send_all 和 send 方法）
            self.notifier.send_all("Linux.Do 签到结果", base_msg)
            logger.info("通知发送成功")
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")

    def run(self):
        """主执行流程"""
        login_success = False
        try:
            # 优先Cookie登录
            if COOKIES:
                login_success = self.login_with_cookies(COOKIES)
                if not login_success:
                    logger.warning("Cookie登录失败，尝试账号密码登录...")
            
            # Cookie登录失败则用账号密码
            if not login_success and USERNAME and PASSWORD:
                login_success = self.login()
            
            if not login_success:
                raise Exception("所有登录方式均失败")
            
            # 执行浏览任务
            browse_success = True
            if BROWSE_ENABLED:
                browse_success = self.click_topic()
            
            # 发送通知
            self.send_notifications(browse_success)
            
        except Exception as e:
            logger.error(f"程序执行失败: {str(e)}")
            # 发送失败通知
            self.notifier.send_all("Linux.Do 签到失败", f"❌ 执行失败: {str(e)}\n账号: {USERNAME}")
        finally:
            # 确保浏览器关闭
            try:
                self.page.close()
                self.browser.quit()
                logger.info("浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器失败: {str(e)}")


if __name__ == "__main__":
    # 前置检查
    if not COOKIES and (not USERNAME or not PASSWORD):
        logger.error("请设置 LINUXDO_COOKIES（Cookie登录），或同时设置 USERNAME 和 PASSWORD（账号密码登录）")
        exit(1)
    
    # 程序启动前随机延迟（避免整点高并发）
    start_delay = random.uniform(10, 30)
    logger.info(f"程序启动延迟 {start_delay:.2f}s，避免触发限流")
    time.sleep(start_delay)
    
    # 执行主程序
    browser = LinuxDoBrowser()
    browser.run()
