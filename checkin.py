#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'


def load_balance_hash():
    """加载余额hash"""
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def save_balance_hash(balance_hash):
    """保存余额hash"""
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
    """生成余额数据的hash"""
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
    """解析 cookies 数据"""
    if isinstance(cookies_data, dict):
        return cookies_data

    if isinstance(cookies_data, str):
        # 兼容处理：如果是 session=xxx 格式，提取 xxx；如果直接是 xxx，则保留
        if cookies_data.startswith('session='):
            return {'session': cookies_data.split('=', 1)[1]}
        
        cookies_dict = {}
        for cookie in cookies_data.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
        return cookies_dict
    return {}


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
    """使用 Playwright 获取 WAF cookies"""
    print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')
    async with async_playwright() as p:
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=False,
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0',
                viewport={'width': 1920, 'height': 1080},
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ],
            )
            page = await context.new_page()
            try:
                await page.goto(login_url, wait_until='networkidle')
                try:
                    await page.wait_for_function('document.readyState === "complete"', timeout=5000)
                except Exception:
                    await page.wait_for_timeout(3000)
                cookies = await page.context.cookies()
                waf_cookies = {c.get('name'): c.get('value') for c in cookies if c.get('name') in required_cookies}
                await context.close()
                return waf_cookies
            except Exception as e:
                print(f'[FAILED] {account_name}: WAF error: {e}')
                await context.close()
                return None


def get_user_info(client, headers, user_info_url: str):
    """获取用户信息"""
    try:
        response = client.get(user_info_url, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                user_data = data.get('data', {})
                quota = round(user_data.get('quota', 0) / 500000, 2)
                used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
                return {
                    'success': True,
                    'quota': quota,
                    'used_quota': used_quota,
                    'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
                }
        return {'success': False, 'error': f'HTTP {response.status_code}: {response.text[:50]}'}
    except Exception as e:
        return {'success': False, 'error': f'Failed: {str(e)[:50]}'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
    """准备请求所需的 cookies"""
    if provider_config.needs_waf_cookies():
        login_url = f'{provider_config.domain}{provider_config.login_path}'
        waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
        if not waf_cookies: return None
        return {**waf_cookies, **user_cookies}
    return user_cookies


def execute_check_in(client, account_name: str, provider_config, headers: dict):
    """执行签到请求"""
    print(f'[NETWORK] {account_name}: Executing check-in')
    sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
    checkin_headers = headers.copy()
    checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
    
    response = client.post(sign_in_url, headers=checkin_headers, timeout=30)
    print(f'[RESPONSE] {account_name}: Status {response.status_code}')

    if response.status_code == 200:
        try:
            result = response.json()
            if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
                print(f'[SUCCESS] {account_name}: Check-in successful!')
                return True
            else:
                print(f'[FAILED] {account_name}: {result.get("msg", "Unknown error")}')
                return False
        except:
            return 'success' in response.text.lower()
    return False


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    """为单个账号执行签到操作"""
    account_name = account.get_display_name(account_index)
    provider_config = app_config.get_provider(account.provider)
    if not provider_config: return False, None

    # --- 修复逻辑开始 ---
    # 检查 account.cookies 的类型
    raw_cookie_data = account.cookies
    
    if isinstance(raw_cookie_data, dict):
        # 如果是字典，将其转为 key=value; 格式的字符串
        raw_cookie = "; ".join([f"{k}={v}" for k, v in raw_cookie_data.items()])
    else:
        # 如果是字符串，直接使用并去除空格
        raw_cookie = str(raw_cookie_data).strip()

    # 处理 session= 前缀逻辑
    if "session=" in raw_cookie:
        # 提取 session= 之后的部分，防止重复嵌套
        import re
        match = re.search(r'session=([^;]+)', raw_cookie)
        session_val = match.group(1) if match else raw_cookie
    else:
        session_val = raw_cookie

    formatted_cookie = f'session={session_val}'
    # --- 修复逻辑结束 ---

    client = httpx.Client(http2=True, timeout=30.0)
    try:
        headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'new-api-user': str(account.api_user),
            'referer': f'{provider_config.domain}/console/personal',
            'sec-ch-ua': '"Not(A:Brand";v="8", "Chromium";v="144", "Microsoft Edge";v="144"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0',
            'cookie': formatted_cookie
        }
        # ... 后续逻辑保持不变
        user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
        user_info = get_user_info(client, headers, user_info_url)
        
        if user_info and user_info.get('success'):
            print(user_info['display'])
            success = execute_check_in(client, account_name, provider_config, headers)
            return success, user_info
        return False, user_info
    except Exception as e:
        print(f'[FAILED] {account_name}: {e}')
        return False, None
    finally:
        client.close()


async def main():
    """主函数"""
    print('[SYSTEM] AnyRouter.top auto check-in script started')
    app_config = AppConfig.load_from_env()
    accounts = load_accounts_config()
    if not accounts: sys.exit(1)

    last_balance_hash = load_balance_hash()
    success_count, total_count = 0, len(accounts)
    notification_content, current_balances = [], {}
    need_notify, balance_changed = False, False

    for i, account in enumerate(accounts):
        account_key = f'account_{i + 1}'
        try:
            success, user_info = await check_in_account(account, i, app_config)
            if success: success_count += 1
            
            if not success:
                need_notify = True
                status = '[FAIL]'
            else:
                status = '[SUCCESS]'

            if user_info and user_info.get('success'):
                current_balances[account_key] = {'quota': user_info['quota'], 'used': user_info['used_quota']}
            
            if not success or (user_info and user_info.get('success')):
                account_res = f'{status} {account.get_display_name(i)}'
                if user_info: account_res += f'\n{user_info.get("display", user_info.get("error"))}'
                notification_content.append(account_res)
        except Exception as e:
            need_notify = True
            notification_content.append(f'[FAIL] {account.get_display_name(i)} exception: {e}')

    skip_notify = os.getenv('SKIP_NOTIFY', 'false').lower() in ('true', '1', 'yes')
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    
    if current_balance_hash:
        if last_balance_hash and current_balance_hash != last_balance_hash:
            balance_changed = True
        save_balance_hash(current_balance_hash)

    if need_notify and notification_content:
        time_info = f'[TIME] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        summary = f'\n\n[STATS] Success: {success_count}/{total_count}'
        notify_content = f'{time_info}\n\n' + '\n'.join(notification_content) + summary
        
        print(notify_content)
        if not skip_notify:
            notify.push_message('AnyRouter Check-in FAILURE Alert', notify_content, msg_type='text')
    
    sys.exit(0 if success_count == total_count else 1)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        print(f'Critical Error: {e}')
        sys.exit(1)
