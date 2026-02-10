#!/usr/bin/env python3
"""
AnyRouter.top 多账号自动签到脚本
整合 WAF 绕过、详细日志与分站点 Provider 逻辑
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
    """将多种格式的 Cookie 转为字典"""
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
    """使用 Playwright 模拟浏览器绕过 WAF 挑战"""
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
                # 等待 Cloudflare 或其他挑战完成
                await asyncio.sleep(5) 
                cookies = await page.context.cookies()
                waf_cookies = {c['name']: c['value'] for c in cookies if c['name'] in required_cookies or 'cf' in c['name'].lower()}
                print(f'[WAF] {account_name}: 成功获取到 {len(waf_cookies)} 个 WAF 相关 Cookies')
                await context.close()
                return waf_cookies
            except Exception as e:
                print(f'[FAILED] {account_name}: WAF 挑战异常: {e}')
                await context.close()
                return None

async def prepare_all_cookies(account_name, provider_config, user_cookies_dict):
    """整合用户 Cookie 与 WAF Cookie"""
    if provider_config.needs_waf_cookies():
        login_url = f"{provider_config.domain}/login"
        waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
        if waf_cookies:
            return {**user_cookies_dict, **waf_cookies}
    return user_cookies_dict

def get_user_info(client, headers, url):
    """获取用户信息并解析余额"""
    try:
        res = client.get(url, headers=headers, timeout=30)
        if res.status_code == 200:
            data = res.json()
            if data.get('success'):
                u = data.get('data', {})
                # 转换单位 500000 = $1.00
                q = round(u.get('quota', 0) / 500000, 2)
                used = round(u.get('used_quota', 0) / 500000, 2)
                return {'success': True, 'quota': q, 'used_quota': used, 'display': f'💰 余额: ${q} | 已用: ${used}'}
        return {'success': False, 'error': f'HTTP {res.status_code}: {res.text[:50]}'}
    except Exception as e:
        return {'success': False, 'error': f'解析失败: {str(e)[:50]}'}

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    provider_name = account.provider
    provider_config = app_config.get_provider(provider_name)
    
    if not provider_config:
        print(f'[ERROR] {account_name}: 未找到 Provider "{provider_name}" 配置')
        return False, None

    print(f"\n{'-'*30}\n[账号] {account_name}\n[站点] {provider_name} ({provider_config.domain})\n{'-'*30}")

    # 1. 处理 Cookie
    user_cookies_dict = parse_cookies_to_dict(account.cookies)
    all_cookies_dict = await prepare_all_cookies(account_name, provider_config, user_cookies_dict)
    
    # 特殊处理：确保 session= 存在
    session_val = all_cookies_dict.get('session')
    if not session_val:
        # 尝试从原始数据中提取
        raw_str = str(account.cookies)
        match = re.search(r'session=([^;]+)', raw_str)
        session_val = match.group(1) if match else raw_str.strip()

    cookie_header = f"session={session_val}; " + "; ".join([f"{k}={v}" for k, v in all_cookies_dict.items() if k != 'session'])

    headers = {
        'accept': 'application/json, text/plain, */*',
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
        try:
            # 2. 获取信息
            info_url = f"{provider_config.domain}{provider_config.user_info_path}"
            print(f'[步骤 1] 正在请求用户信息: {provider_config.user_info_path}')
            user_info = get_user_info(client, headers, info_url)
            
            if not user_info.get('success'):
                print(f'   ❌ 认证失败: {user_info.get("error")}')
                return False, user_info
            
            print(f"   ✅ {user_info['display']}")

            # 3. 执行签到
            checkin_url = f"{provider_config.domain}{provider_config.sign_in_path}"
            print(f'[步骤 2] 正在执行签到请求: {provider_config.sign_in_path}')
            
            checkin_headers = headers.copy()
            checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
            
            res = client.post(checkin_url, headers=checkin_headers)
            print(f"   📡 状态码: {res.status_code}")
            
            success = False
            try:
                res_data = res.json()
                print(f"   📝 响应: {json.dumps(res_data, ensure_ascii=False)}")
                if res_data.get('ret') == 1 or res_data.get('code') == 0 or res_data.get('success'):
                    success = True
            except:
                success = 'success' in res.text.lower()
            
            if success: print(f"   🎉 {account_name} 签到成功")
            else: print(f"   ⚠️ {account_name} 签到未成功，可能是重复签到")
            
            return success, user_info

        except Exception as e:
            print(f'   💥 运行异常: {e}')
            return False, None

async def main():
    print(f'[SYSTEM] 脚本启动 | 时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    app_config = AppConfig.load_from_env()
    accounts = load_accounts_config()
    if not accounts: sys.exit(1)

    last_hash = load_balance_hash()
    success_count, total_count = 0, len(accounts)
    notify_list, current_balances = [], {}
    need_push, balance_changed = False, False

    for i, acc in enumerate(accounts):
        ok, info = await check_in_account(acc, i, app_config)
        if ok: success_count += 1
        
        # 统计余额
        if info and info.get('success'):
            current_balances[f'acc_{i}'] = {'quota': info['quota'], 'used': info['used_quota']}
            status_str = "[SUCCESS]" if ok else "[FAIL]"
            notify_list.append(f"{status_str} {acc.get_display_name(i)}\n{info['display']}")
        else:
            need_push = True # 失败必须通知
            notify_list.append(f"[FAIL] {acc.get_display_name(i)}\n原因: {info.get('error') if info else '未知错误'}")

    # 检查余额变动
    curr_hash = generate_balance_hash(current_balances)
    if curr_hash != last_hash:
        balance_changed = True
        save_balance_hash(curr_hash)

    # 推送策略：失败才通知
    skip_notify = os.getenv('SKIP_NOTIFY', 'false').lower() in ('true', '1', 'yes')
    
    if need_push and not skip_notify:
        content = "\n\n".join(notify_list) + f"\n\n[统计] 成功: {success_count}/{total_count}"
        notify.push_message('AnyRouter 签到异常告警', content, msg_type='text')
        print('\n[NOTIFY] 已发送失败告警通知')
    else:
        print(f'\n[INFO] 任务执行完毕 (成功: {success_count}/{total_count})，无须通知或已跳过。')

    sys.exit(0 if success_count == total_count else 1)

if __name__ == '__main__':
    asyncio.run(main())
