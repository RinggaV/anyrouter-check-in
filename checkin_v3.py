#!/usr/bin/env python3
"""
AnyRouter 自动签到脚本 V3
终极改进：添加页面交互模拟来触发 Cloudflare Turnstile 验证

改进策略：
1. 模拟真实用户行为（鼠标移动、点击）
2. 主动触发 Turnstile iframe
3. 智能等待验证完成
4. 快速失败机制
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

from utils.config_v2 import AccountConfig, AppConfig, load_accounts_config
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

async def simulate_user_interaction(page, account_name: str):
    """
    模拟真实用户行为来触发 Cloudflare Turnstile 验证
    """
    try:
        print(f'[WAF] {account_name}: 模拟用户交互...')

        # 1. 模拟鼠标移动
        await page.mouse.move(100, 100)
        await asyncio.sleep(0.3)
        await page.mouse.move(300, 200)
        await asyncio.sleep(0.3)
        await page.mouse.move(500, 300)
        await asyncio.sleep(0.5)

        # 2. 尝试查找并点击 Turnstile checkbox
        try:
            # Turnstile 通常在 iframe 中
            turnstile_frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]').first

            # 等待 iframe 加载
            await asyncio.sleep(1)

            # 点击 checkbox
            checkbox = turnstile_frame.locator('input[type="checkbox"]').first
            if await checkbox.is_visible(timeout=2000):
                print(f'[WAF] {account_name}: 找到 Turnstile checkbox，尝试点击...')
                await checkbox.click()
                await asyncio.sleep(1)
            else:
                # 如果找不到 checkbox，尝试点击整个 iframe 区域
                print(f'[WAF] {account_name}: 尝试点击 Turnstile 区域...')
                await turnstile_frame.locator('body').click()
                await asyncio.sleep(1)
        except Exception as e:
            print(f'[WAF] {account_name}: Turnstile 交互失败: {e}')

        # 3. 模拟页面滚动
        await page.evaluate('window.scrollTo(0, 100)')
        await asyncio.sleep(0.3)
        await page.evaluate('window.scrollTo(0, 0)')

    except Exception as e:
        print(f'[WAF] {account_name}: 用户交互模拟失败: {e}')

async def get_waf_bypass_data(account_name: str, domain: str, max_wait: int = 40):
    """
    获取 WAF 绕过所需的数据（cookies 和可选的 Turnstile token）

    改进策略：
    1. 访问页面
    2. 模拟用户交互触发 Turnstile
    3. 智能等待 token 生成
    4. 快速失败机制
    """
    print(f'[WAF] {account_name}: 启动浏览器获取 WAF 数据...')

    try:
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
                    # 1. 访问页面触发 WAF
                    print(f'[WAF] {account_name}: 访问页面...')
                    await page.goto(f"{domain}/console/personal", wait_until='networkidle', timeout=60000)

                    # 2. 等待页面完全加载
                    await asyncio.sleep(2)

                    # 3. 检查 Turnstile 是否存在
                    turnstile_exists = await page.evaluate("typeof turnstile !== 'undefined'")

                    token = ""
                    if turnstile_exists:
                        print(f'[WAF] {account_name}: 检测到 Turnstile')

                        # 4. 模拟用户交互触发验证
                        await simulate_user_interaction(page, account_name)

                        # 5. 等待 Turnstile token 生成
                        print(f'[WAF] {account_name}: 等待 Turnstile 验证完成...')
                        check_interval = 2
                        checks = 0
                        max_checks = max_wait // check_interval

                        for i in range(max_checks):
                            await asyncio.sleep(check_interval)
                            checks += 1

                            try:
                                token = await page.evaluate("turnstile.getResponse()")
                                if token:
                                    elapsed = checks * check_interval + 2
                                    print(f'[WAF] {account_name}: ✅ 获取到 Turnstile Token (耗时 {elapsed}s)')
                                    break
                                elif checks % 5 == 0:  # 每 10 秒打印一次
                                    print(f'[WAF] {account_name}: 等待中... ({checks * check_interval}s)')
                            except Exception as e:
                                if checks == 1:
                                    print(f'[WAF] {account_name}: Token 读取异常: {e}')

                        if not token:
                            print(f'[WAF] {account_name}: ⚠️ Turnstile Token 未获取到（超时 {max_wait}s）')
                    else:
                        print(f'[WAF] {account_name}: 未检测到 Turnstile，仅获取 cookies')

                    # 6. 获取所有 cookies
                    cookies_list = await page.context.cookies()
                    waf_cookies = {c['name']: c['value'] for c in cookies_list}

                    await context.close()

                    print(f'[WAF] {account_name}: ✅ 成功获取 WAF 数据 (cookies: {len(waf_cookies)}, token: {"有" if token else "无"})')
                    return {'cookies': waf_cookies, 'token': token}

                except Exception as e:
                    print(f'[WAF] {account_name}: ❌ 页面操作失败: {e}')
                    await context.close()
                    return None

    except Exception as e:
        print(f'[WAF] {account_name}: ❌ 浏览器启动失败: {e}')
        return None

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    provider_config = app_config.get_provider(account.provider)

    if not provider_config:
        print(f"[ERROR] {account_name}: 未找到 provider 配置: {account.provider}")
        return False, None

    print(f"\n{'-'*30}\n[账号] {account_name}\n[站点] {account.provider}\n{'-'*30}")

    # 判断是否需要 WAF 绕过
    needs_waf = provider_config.bypass_method == 'waf_cookies'
    user_cookies_data = account.cookies
    waf_data = None

    if needs_waf:
        waf_data = await get_waf_bypass_data(account_name, provider_config.domain)
        if not waf_data:
            print(f"   ❌ WAF 绕过失败")
            return False, {'success': False, 'error': 'WAF bypass failed'}

    # 构造 cookies
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
                    error_msg = data.get('message', '未知错误')
                    print(f"   ❌ 获取用户信息失败: {error_msg}")
                    return False, {'success': False, 'error': error_msg}
            else:
                error_msg = f'HTTP {res_info.status_code}'
                print(f"   ❌ 请求失败: {error_msg}")
                return False, {'success': False, 'error': error_msg}
        except Exception as e:
            error_msg = str(e)
            print(f"   ❌ 请求异常: {error_msg}")
            return False, {'success': False, 'error': error_msg}

        # 执行签到
        if not provider_config.sign_in_path:
            print(f"   ✅ 签到成功 (无需调用签到接口)")
            return True, user_info

        payload = {}
        if waf_data and waf_data.get('token'):
            payload['token'] = waf_data['token']
            print(f"   🔑 使用 Turnstile Token")

        try:
            checkin_url = f"{provider_config.domain}{provider_config.sign_in_path}"
            checkin_headers = headers.copy()
            checkin_headers['Content-Type'] = 'application/json'

            res_chk = await client.post(checkin_url, headers=checkin_headers, json=payload)
            res_json = res_chk.json()
            msg = res_json.get('message', '') or res_json.get('msg', '')
            is_done = any(k in msg for k in ["今日已签到", "重复签到", "已经签到"])

            if res_json.get('success') or is_done:
                if is_done:
                    print(f"   ℹ️ 重复签到 (成功)")
                else:
                    print(f"   ✅ 签到成功")
                return True, user_info
            else:
                print(f"   ❌ 签到失败: {msg}")
                return False, user_info
        except Exception as e:
            print(f"   ❌ 签到请求异常: {str(e)}")
            return False, user_info

async def main():
    print(f'[SYSTEM] AnyRouter 自动签到启动 V3 (带交互模拟)')
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

    print(f'\n[SYSTEM] 签到完成: {success_count}/{total_count} 成功')
    sys.exit(0 if success_count == total_count else 1)

if __name__ == '__main__':
    asyncio.run(main())
