#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 - 适配版
支持 bypass_method: waf_cookies 配置检测与 Turnstile Token 自动注入
"""

import asyncio
import hashlib
import json
import os
import sys
import re
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'
COMMON_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0'

def load_balance_hash():
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception: pass
    return None

def save_balance_hash(balance_hash):
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'[WARN] 保存余额hash失败: {e}')

def generate_balance_hash(balances):
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]

def parse_cookies_to_dict(cookies_data):
    if isinstance(cookies_data, dict):
        return cookies_data
    cookies_dict = {}
    if isinstance(cookies_data, str):
        for cookie in cookies_data.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
    return cookies_dict

async def get_waf_data_with_playwright(account_name: str, target_url: str):
    """使用 Playwright 绕过 WAF 并获取 Token"""
    print(f'[WAF] {account_name}: 启动浏览器绕过防护...')
    async with async_playwright() as p:
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=True,
                user_agent=COMMON_UA,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            page = await context.new_page()
            try:
                # 访问目标地址，针对 Cloudflare 质询等待
                await page.goto(target_url, wait_until='networkidle', timeout=60000)
                print(f'[WAF] {account_name}: 等待质询挑战完成 (15s)...')
                await asyncio.sleep(15) 
                
                # 尝试截取 Turnstile Token
                token = await page.evaluate("typeof turnstile !== 'undefined' ? turnstile.getResponse() : ''")
                
                cookies_list = await page.context.cookies()
                waf_cookies = {c['name']: c['value'] for c in cookies_list}
                
                await context.close()
                return {'cookies': waf_cookies, 'token': token}
            except Exception as e:
                print(f'[FAILED] {account_name}: 浏览器操作失败: {e}')
                await context.close()
                return None

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    provider_config = app_config.get_provider(account.provider)
    if not provider_config: return False, None

    print(f"\n{'-'*30}\n[账号] {account_name}\n[站点] {account.provider} ({provider_config.domain})\n{'-'*30}")

    # 判断是否需要 WAF 绕过 (根据你的 bypass_method 配置)
    # 注意：这里需要确保你的 AppConfig 映射了 bypass_method 字段
    needs_waf = getattr(provider_config, 'bypass_method', '') == 'waf_cookies'
    
    user_cookies_dict = parse_cookies_to_dict(account.cookies)
    waf_data = None
    
    if needs_waf:
        # 使用个人中心路径触发挑战
        waf_data = await get_waf_data_with_playwright(account_name, f"{provider_config.domain}/console/personal")
        if waf_data:
            user_cookies_dict.update(waf_data['cookies'])
            if waf_data['token']: print(f'   ✅ 成功截获 Turnstile Token')

    # 构造 Session Cookie
    session_val = user_cookies_dict.get('session')
    if not session_val:
        match = re.search(r'session=([^;]+)', str(account.cookies))
        session_val = match.group(1) if match else str(account.cookies).strip()

    cookie_header = f"session={session_val}; " + "; ".join([f"{k}={v}" for k, v in user_cookies_dict.items() if k != 'session'])

    headers = {
        'accept': 'application/json, text/plain, */*',
        'new-api-user': str(account.api_user),
        'referer': f'{provider_config.domain}/console/personal',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': COMMON_UA,
        'cookie': cookie_header
    }

    async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
        # 1. 获取信息
        info_url = f"{provider_config.domain}{provider_config.user_info_path}"
        print(f'[步骤 1] 正在请求用户信息: {provider_config.user_info_path}')
        try:
            res_info = await client.get(info_url, headers=headers)
            if res_info.status_code == 200 and res_info.json().get('success'):
                u = res_info.json().get('data', {})
                q, used = round(u.get('quota', 0)/500000, 2), round(u.get('used_quota', 0)/500000, 2)
                user_info = {'success': True, 'quota': q, 'used_quota': used, 'display': f'💰 余额: ${q} | 已用: ${used}'}
                print(f"   ✅ {user_info['display']}")
            else:
                print(f"   ❌ 认证失败: HTTP {res_info.status_code}")
                return False, {'success': False, 'error': f'HTTP {res_info.status_code}'}
        except Exception as e:
            return False, {'success': False, 'error': str(e)}

        # 2. 执行签到
        checkin_url = f"{provider_config.domain}{provider_config.sign_in_path}"
        print(f'[步骤 2] 正在请求签到: {provider_config.sign_in_path}')
        
        payload = {}
        if waf_data and waf_data['token']:
            payload['token'] = waf_data['token'] # 注入 Turnstile Token

        try:
            checkin_headers = headers.copy()
            checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
            res_chk = await client.post(checkin_url, headers=checkin_headers, json=payload)
            print(f"   📡 状态码: {res_chk.status_code}")
            
            res_json = res_chk.json()
            msg = res_json.get('message', '') or res_json.get('msg', '')
            is_done = any(k in msg for k in ["今日已签到", "重复签到", "已经签到"])
            
            if res_json.get('success') or res_json.get('ret') == 1 or is_done:
                if is_done: print(f"   ℹ️ 重复签到判定为成功")
                return True, user_info
            else:
                print(f"   ❌ 失败响应: {json.dumps(res_json, ensure_ascii=False)}")
                return False, user_info
        except Exception as e:
            print(f"   💥 签到异常: {e}")
            return False, user_info

async def main():
    print(f'[SYSTEM] AnyRouter 自动签到启动 | {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    app_config = AppConfig.load_from_env()
    accounts = load_accounts_config()
    if not accounts: sys.exit(1)

    last_hash = load_balance_hash()
    success_count, total_count = 0, len(accounts)
    notify_list, current_balances = [], {}
    need_push = False

    for i, acc in enumerate(accounts):
        ok, info = await check_in_account(acc, i, app_config)
        if ok: success_count += 1
        else: need_push = True
        
        status = "[SUCCESS]" if ok else "[FAIL]"
        if info and info.get('success'):
            current_balances[f'acc_{i}'] = {'quota': info['quota']}
            notify_list.append(f"{status} {acc.get_display_name(i)}\n{info['display']}")
        else:
            notify_list.append(f"{status} {acc.get_display_name(i)}\n原因: {info.get('error') if info else '未知'}")

    curr_hash = generate_balance_hash(current_balances)
    if curr_hash != last_hash: save_balance_hash(curr_hash)

    skip_notify = os.getenv('SKIP_NOTIFY', 'false').lower() in ('true', '1', 'yes')
    if need_push and not skip_notify:
        notify.push_message('AnyRouter 签到结果报告', "\n\n".join(notify_list), msg_type='text')
    sys.exit(0)
    # sys.exit(0 if success_count == total_count else 1)

if __name__ == '__main__':
    asyncio.run(main())
