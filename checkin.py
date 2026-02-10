#!/usr/bin/env python3
"""
AnyRouter.top 多账号自动签到脚本
优化：判定“今日已签到”为成功，失败打印原始响应
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

# 常量配置
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
        print(f'[WARN] Failed to save balance hash: {e}')

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

async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
    print(f'[WAF] {account_name}: 正在启动浏览器环境绕过防护...')
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
                await page.goto(login_url, wait_until='networkidle')
                await asyncio.sleep(5) 
                cookies = await page.context.cookies()
                waf_cookies = {c['name']: c['value'] for c in cookies if c['name'] in required_cookies or 'cf' in c['name'].lower()}
                print(f'[WAF] {account_name}: 成功获取到 {len(waf_cookies)} 个 Cookies')
                await context.close()
                return waf_cookies
            except Exception as e:
                print(f'[FAILED] {account_name}: WAF 挑战异常: {e}')
                await context.close()
                return None

async def prepare_all_cookies(account_name, provider_config, user_cookies_dict):
    if provider_config.needs_waf_cookies():
        login_url = f"{provider_config.domain}/login"
        waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
        if waf_cookies:
            return {**user_cookies_dict, **waf_cookies}
    return user_cookies_dict

async def get_user_info(client, headers, url):
    try:
        res = await client.get(url, headers=headers, timeout=30)
        if res.status_code == 200:
            data = res.json()
            if data.get('success'):
                u = data.get('data', {})
                q = round(u.get('quota', 0) / 500000, 2)
                used = round(u.get('used_quota', 0) / 500000, 2)
                return {'success': True, 'quota': q, 'used_quota': used, 'display': f'💰 余额: ${q} | 已用: ${used}'}
        return {'success': False, 'error': f'HTTP {res.status_code}', 'raw': res.text}
    except Exception as e:
        return {'success': False, 'error': f'请求失败: {str(e)[:50]}'}

async def execute_check_in(client, account_name, provider_config, headers):
    checkin_url = f"{provider_config.domain}{provider_config.sign_in_path}"
    print(f'[步骤 2] 正在请求签到: {provider_config.sign_in_path}')
    
    checkin_headers = headers.copy()
    checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
    
    try:
        res = await client.post(checkin_url, headers=checkin_headers, timeout=30)
        print(f"   📡 状态码: {res.status_code}")
        
        try:
            res_data = res.json()
            msg = res_data.get('message', '') or res_data.get('msg', '')
            
            # 逻辑：成功标志位 OR 包含“今日已签到”
            is_success_msg = any(keyword in msg for keyword in ["今日已签到", "已经签到", "重复签到"])
            if res_data.get('ret') == 1 or res_data.get('code') == 0 or res_data.get('success') or is_success_msg:
                if is_success_msg: print(f"   ℹ️ {account_name}: {msg} (判定为成功)")
                return True
            else:
                print(f"   ❌ 签到失败响应: {json.dumps(res_data, ensure_ascii=False)}")
                return False
        except:
            # 非 JSON 响应的模糊匹配
            raw_text = res.text
            if 'success' in raw_text.lower() or '今日已签到' in raw_text:
                return True
            print(f"   ❌ 原始失败响应: {raw_text}")
            return False
    except Exception as e:
        print(f"   💥 签到异常: {e}")
    return False

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    provider_config = app_config.get_provider(account.provider)
    if not provider_config: return False, None

    print(f"\n{'-'*30}\n[账号] {account_name}\n[站点] {account.provider} ({provider_config.domain})\n{'-'*30}")

    user_cookies_dict = parse_cookies_to_dict(account.cookies)
    all_cookies_dict = await prepare_all_cookies(account_name, provider_config, user_cookies_dict)
    
    # 提取 session 值
    session_val = all_cookies_dict.get('session')
    if not session_val:
        raw_str = str(account.cookies)
        match = re.search(r'session=([^;]+)', raw_str)
        session_val = match.group(1) if match else raw_str.strip()

    cookie_header = f"session={session_val}; " + "; ".join([f"{k}={v}" for k, v in all_cookies_dict.items() if k != 'session'])

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
        info_url = f"{provider_config.domain}{provider_config.user_info_path}"
        print(f'[步骤 1] 正在请求用户信息: {provider_config.user_info_path}')
        user_info = await get_user_info(client, headers, info_url)
        
        if not user_info.get('success'):
            print(f'   ❌ 认证失败: {user_info.get("error")}')
            if user_info.get('raw'): print(f'   📄 原始响应: {user_info["raw"][:200]}')
            return False, user_info
        
        print(f"   ✅ {user_info['display']}")
        success = await execute_check_in(client, account_name, provider_config, headers)
        return success, user_info

async def main():
    print(f'[SYSTEM] 脚本启动 | 时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
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
        
        status_str = "[SUCCESS]" if ok else "[FAIL]"
        if not ok: need_push = True
        
        if info and info.get('success'):
            current_balances[f'acc_{i}'] = {'quota': info['quota'], 'used': info['used_quota']}
            notify_list.append(f"{status_str} {acc.get_display_name(i)}\n{info['display']}")
        else:
            notify_list.append(f"{status_str} {acc.get_display_name(i)}\n原因: {info.get('error') if info else '异常'}")

    curr_hash = generate_balance_hash(current_balances)
    if curr_hash != last_hash:
        save_balance_hash(curr_hash)

    skip_notify = os.getenv('SKIP_NOTIFY', 'false').lower() in ('true', '1', 'yes')
    if need_push and not skip_notify:
        content = "\n\n".join(notify_list) + f"\n\n[统计] 成功: {success_count}/{total_count}"
        notify.push_message('AnyRouter 签到结果告警', content, msg_type='text')
    
    sys.exit(0 if success_count == total_count else 1)

if __name__ == '__main__':
    asyncio.run(main())
