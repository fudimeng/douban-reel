"""Exercise the actual running UI. Reads local .env without printing credentials."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
env = dict(line.split('=', 1) for line in (root / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
output = root / 'artifacts'
output.mkdir(exist_ok=True)
base_url = 'http://127.0.0.1:' + env.get('PORT', '8787')

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context(http_credentials={'username': env.get('ADMIN_USERNAME', 'admin'), 'password': env['ADMIN_PASSWORD']}, viewport={'width':1440, 'height':1100}, locale='zh-CN')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base_url)
    expect(page.locator('#schedulerState')).not_to_have_text('读取中')
    page.locator('#history_days').fill('47')
    page.get_by_role('button', name='保存配置', exact=True).click()
    expect(page.locator('#notice')).to_have_text('配置已保存')
    page.reload()
    expect(page.locator('#history_days')).to_have_value('47')
    page.locator('#history_days').fill('30')
    page.get_by_role('button', name='保存配置', exact=True).click()
    expect(page.locator('#notice')).to_have_text('配置已保存')
    page.locator('#preview').click()
    expect(page.locator('#notice')).to_contain_text('请先配置')
    assert not errors, errors
    page.reload()
    expect(page.locator('#schedulerState')).not_to_have_text('读取中')
    page.screenshot(path=str(output / 'desktop.png'), full_page=True)
    page.set_viewport_size({'width':390, 'height':844})
    page.screenshot(path=str(output / 'mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
    assert not errors, errors
    print(json.dumps({'browser':'Chromium','config_persists':True,'incomplete_sync_rejected':True,'mobile_overflow':False,'javascript_errors':errors,'screenshots':['artifacts/desktop.png','artifacts/mobile.png']},ensure_ascii=False))
    browser.close()
