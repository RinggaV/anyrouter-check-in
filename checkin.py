#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
修复配置读取逻辑，支持 Turnstile Token 注入与 WAF 绕过
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
        print(f'[WARN] 余额hash保存失败: {e}')

def generate_balance_hash(balances):
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]

async def get_interactive_waf_data(account_name: str, domain: str):
    """
    通过模拟点击和显式等待绕过交互式质询
    """
    print(f'[WAF] {account_name}: 启动交互式浏览器环境...')
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
                # 访问个人中心页面触发质询
                await page.goto(f"{domain}/console/personal", wait_until='networkidle', timeout=60000)
                print(f'[WAF] {account_name}: 等待 Cloudflare 质询 (15s)...')
                await asyncio.sleep(15) 
                
                # 截获 Token
                token = await page.evaluate("typeof turnstile !== 'undefined' ? turnstile.getResponse() : ''")
                cookies_list = await page.context.cookies()
                waf_cookies = {c['name']: c['value'] for c in cookies_list}
                
                await context.close()
                return {'cookies': waf_cookies, 'token': token}
            except Exception as e:
                print(f'[FAILED] {account_name}: 浏览器交互失败: {e}')
                await context.close()
                return None

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    provider_config = app_config.get_provider(account.provider)
    if not provider_config: return False, None

    print(f"\n{'-'*30}\n[账号] {account_name}\n[站点] {account.provider}\n{'-'*30}")

    # --- 修正点：支持从配置中读取 bypass_method ---
    bypass_method = ""
    if hasattr(app_config, 'providers_raw'):
        raw_info = app_config.providers_raw.get(account.provider, {})
        bypass_method = raw_info.get('bypass_method', '')
    else:
        bypass_method = getattr(provider_config, 'bypass_method', '')
    
    needs_waf = bypass_method == 'waf_cookies'
    user_cookies_data = account.cookies
    waf_data = None
    
    if needs_waf:
        waf_data = await get_interactive_waf_data(account_name, provider_config.domain)

    # --- 融合 Cookie 构造逻辑 ---
    final_cookies_dict = {}
    if isinstance(user_cookies_data, dict):
        final_cookies_dict.update(user_cookies_data)
    elif isinstance(user_cookies_data, str):
        for part in user_cookies_data.split(';'):
            if '=' in part:
                k, v = part.strip().split('=', 1)
                final_cookies_dict[k] = v
            elif part.strip():
                final_cookies_dict['session'] = part.strip()

    if waf_data and waf_data.get('cookies'):
        final_cookies_dict.update(waf_data['cookies'])

    cookie_header = "; ".join([f"{k}={v}" for k, v in final_cookies_dict.items()])

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'new-api-user': str(account.api_user),
        'referer': f'{provider_config.domain}/console/personal',
        'sec-ch-ua': '"Not(A:Brand";v="8", "Chromium";v="144", "Microsoft Edge";v="144"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': COMMON_UA,
        'cookie': cookie_header
    }

    async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
        # 获取用户信息
        info_url = f"{provider_config.domain}{provider_config.user_info_path}"
        try:
            res_info = await client.get(info_url, headers=headers)
            if res_info.status_code == 200:
                data = res_info.json()
                if data.get('success'):
                    u = data.get('data', {})
                    q = round(u.get('quota', 0)/500000, 2)
                    user_info = {'success': True, 'quota': q, 'used_quota': round(u.get('used_quota', 0)/500000, 2), 'display': f'💰 余额: ${q}'}
                    print(f"   ✅ {user_info['display']}")
                else:
                    return False, {'success': False, 'error': data.get('message')}
            else:
                return False, {'success': False, 'error': f'HTTP {res_info.status_code}'}
        except Exception as e:
            return False, {'success': False, 'error': str(e)}

        # 执行签到
        payload = {}
        if waf_data and waf_data.get('token'):
            payload['token'] = waf_data['token'] # 自动注入 Turnstile Token

        try:
            checkin_url = f"{provider_config.domain}{provider_config.sign_in_path}"
            checkin_headers = headers.copy()
            checkin_headers['Content-Type'] = 'application/json'
            
            res_chk = await client.post(checkin_url, headers=checkin_headers, json=payload)
            res_json = res_chk.json()
            msg = res_json.get('message', '') or res_json.get('msg', '')
            is_done = any(k in msg for k in ["今日已签到", "重复签到", "已经签到"])
            
            if res_json.get('success') or is_done:
                if is_done: print(f"   ℹ️ 重复签到 (成功)")
                return True, user_info
            else:
                print(f"   ❌ 签到失败: {msg}")
                return False, user_info
        except Exception:
            return False, user_info

async def main():
    print(f'[SYSTEM] AnyRouter 自动签到启动')
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
            notify_list.append(f"{status} {acc.get_display_name(i)}")

    curr_hash = generate_balance_hash(current_balances)
    if curr_hash != last_hash: save_balance_hash(curr_hash)

    skip_notify = os.getenv('SKIP_NOTIFY', 'false').lower() in ('true', '1', 'yes')
    if need_push and not skip_notify:
        notify.push_message('AnyRouter 签到结果报告', "\n\n".join(notify_list))
    
    sys.exit(0 if success_count == total_count else 1)

if __name__ == '__main__':
    asyncio.run(main())
