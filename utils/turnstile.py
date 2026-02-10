"""
Turnstile 验证服务类
支持多种求解方式：
1. YesCaptcha API（付费，最可靠）
2. 本地 Turnstile Solver（免费，需要自建）
3. 浏览器自动化（免费，成功率低）
"""
import os
import time
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self):
        """初始化 Turnstile 服务"""
        self.yescaptcha_key = os.getenv('YESCAPTCHA_KEY', '').strip()
        self.solver_url = os.getenv('TURNSTILE_SOLVER_URL', 'http://127.0.0.1:5072')
        self.yescaptcha_api = "https://api.yescaptcha.com"

        # 判断使用哪种方式
        if self.yescaptcha_key:
            self.method = 'yescaptcha'
            print('[Turnstile] 使用 YesCaptcha API')
        elif self._check_solver_available():
            self.method = 'local_solver'
            print('[Turnstile] 使用本地 Turnstile Solver')
        else:
            self.method = 'browser'
            print('[Turnstile] 使用浏览器自动化（成功率较低）')

    def _check_solver_available(self):
        """检查本地 Solver 是否可用"""
        try:
            response = httpx.get(f"{self.solver_url}/health", timeout=2)
            return response.status_code == 200
        except:
            return False

    async def solve_turnstile(self, siteurl: str, sitekey: str, account_name: str = "") -> str:
        """
        求解 Turnstile 验证

        Args:
            siteurl: 网站 URL
            sitekey: Turnstile site key
            account_name: 账号名称（用于日志）

        Returns:
            Turnstile token 或 None
        """
        if self.method == 'yescaptcha':
            return await self._solve_with_yescaptcha(siteurl, sitekey, account_name)
        elif self.method == 'local_solver':
            return await self._solve_with_local_solver(siteurl, sitekey, account_name)
        else:
            # 浏览器自动化方式在主脚本中处理
            return None

    async def _solve_with_yescaptcha(self, siteurl: str, sitekey: str, account_name: str) -> str:
        """使用 YesCaptcha API 求解"""
        try:
            print(f'[YesCaptcha] {account_name}: 创建任务...')

            # 创建任务
            async with httpx.AsyncClient(timeout=30) as client:
                create_url = f"{self.yescaptcha_api}/createTask"
                payload = {
                    "clientKey": self.yescaptcha_key,
                    "task": {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": siteurl,
                        "websiteKey": sitekey
                    }
                }

                response = await client.post(create_url, json=payload)
                response.raise_for_status()
                data = response.json()

                if data.get('errorId') != 0:
                    print(f'[YesCaptcha] {account_name}: 创建任务失败: {data.get("errorDescription")}')
                    return None

                task_id = data['taskId']
                print(f'[YesCaptcha] {account_name}: 任务已创建 (ID: {task_id})')

                # 等待结果
                await asyncio.sleep(5)  # 初始等待

                for attempt in range(30):  # 最多等待 60 秒
                    result_url = f"{self.yescaptcha_api}/getTaskResult"
                    result_payload = {
                        "clientKey": self.yescaptcha_key,
                        "taskId": task_id
                    }

                    response = await client.post(result_url, json=result_payload)
                    response.raise_for_status()
                    data = response.json()

                    if data.get('errorId') != 0:
                        print(f'[YesCaptcha] {account_name}: 获取结果失败: {data.get("errorDescription")}')
                        return None

                    status = data.get('status')
                    if status == 'ready':
                        token = data.get('solution', {}).get('token')
                        if token:
                            print(f'[YesCaptcha] {account_name}: ✅ 成功获取 token')
                            return token
                        else:
                            print(f'[YesCaptcha] {account_name}: 返回结果中没有 token')
                            return None
                    elif status == 'processing':
                        if attempt % 5 == 0:
                            print(f'[YesCaptcha] {account_name}: 处理中... ({attempt * 2}s)')
                        await asyncio.sleep(2)
                    else:
                        print(f'[YesCaptcha] {account_name}: 未知状态: {status}')
                        await asyncio.sleep(2)

                print(f'[YesCaptcha] {account_name}: ⚠️ 超时未获取到 token')
                return None

        except Exception as e:
            print(f'[YesCaptcha] {account_name}: 异常: {e}')
            return None

    async def _solve_with_local_solver(self, siteurl: str, sitekey: str, account_name: str) -> str:
        """使用本地 Turnstile Solver 求解"""
        try:
            print(f'[LocalSolver] {account_name}: 创建任务...')

            async with httpx.AsyncClient(timeout=30) as client:
                # 创建任务
                create_url = f"{self.solver_url}/turnstile?url={siteurl}&sitekey={sitekey}"
                response = await client.get(create_url)
                response.raise_for_status()
                data = response.json()
                task_id = data['taskId']

                print(f'[LocalSolver] {account_name}: 任务已创建 (ID: {task_id})')

                # 等待结果
                await asyncio.sleep(5)

                for attempt in range(30):
                    result_url = f"{self.solver_url}/result?id={task_id}"
                    response = await client.get(result_url)
                    response.raise_for_status()
                    data = response.json()

                    token = data.get('solution', {}).get('token')
                    if token:
                        if token != "CAPTCHA_FAIL":
                            print(f'[LocalSolver] {account_name}: ✅ 成功获取 token')
                            return token
                        else:
                            print(f'[LocalSolver] {account_name}: 验证失败')
                            return None
                    else:
                        if attempt % 5 == 0:
                            print(f'[LocalSolver] {account_name}: 等待中... ({attempt * 2}s)')
                        await asyncio.sleep(2)

                print(f'[LocalSolver] {account_name}: ⚠️ 超时未获取到 token')
                return None

        except Exception as e:
            print(f'[LocalSolver] {account_name}: 异常: {e}')
            return None

    def get_method(self) -> str:
        """获取当前使用的求解方式"""
        return self.method


# 全局实例
turnstile_service = TurnstileService()
