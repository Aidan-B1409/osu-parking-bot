import asyncio
from dataclasses import replace

import pytest

from parking_bot.browser import (
    NetworkWatch,
    Result,
    export_context,
    inspect,
    inspect_loaded,
    new_context,
    retry_after,
)

BUTTON = '<button id="ptypeid_btn_514" value="514" {attrs}>Monthly Permit - Zone A1</button>'


@pytest.mark.parametrize(('attrs', 'expected'), [
    ('', Result.AVAILABLE), ('disabled', Result.UNAVAILABLE),
    ('disabled=""', Result.UNAVAILABLE), ('disabled="false"', Result.UNAVAILABLE),
    ('disabled="disabled"', Result.UNAVAILABLE), ('aria-disabled="true"', Result.UNKNOWN),
    ('style="display:none"', Result.UNKNOWN)])
async def test_boolean_attributes(page, cfg, attrs, expected):
    await page.set_content(BUTTON.format(attrs=attrs))
    assert (await inspect(page, cfg)).result == expected


@pytest.mark.parametrize('html', [
    '', BUTTON.format(attrs='') * 2,
    BUTTON.format(attrs='').replace('Monthly Permit', 'Annual Permit'),
    BUTTON.format(attrs='').replace('value="514"', 'value="123"'),
    '<fieldset disabled>' + BUTTON.format(attrs='') + '</fieldset>',
    BUTTON.format(attrs='') + BUTTON.format(attrs='hidden'),
])
async def test_ambiguous(page, cfg, html):
    await page.set_content(html)
    assert (await inspect(page, cfg)).result == Result.UNKNOWN


async def test_label_whitespace(page, cfg):
    await page.set_content(BUTTON.format(attrs='').replace('Monthly Permit', ' Monthly\n Permit '))
    assert (await inspect(page, cfg)).result == Result.AVAILABLE


async def test_delayed_rendering(page, cfg):
    watch = NetworkWatch(page, cfg)
    await page.set_content('<div id="app"></div>')
    async def render():
        await asyncio.sleep(0.1)
        await page.set_content('<div id="ready">Loaded</div>' + BUTTON.format(attrs='disabled'))
    task = asyncio.create_task(render())
    result = await inspect_loaded(page, cfg, watch)
    await task
    assert result.result == Result.UNAVAILABLE


@pytest.mark.parametrize(('code', 'transient'), [(403, False), (429, True), (500, True), (503, True)])
async def test_http_errors_override_button(page, cfg, code, transient):
    await page.unroute('**/*')
    await page.route('**/*', lambda route: route.fulfill(status=code, headers={'Retry-After': '40000'},
                                                     body='<div id="ready">Loaded</div>' + BUTTON.format(attrs='')))
    watch = NetworkWatch(page, cfg)
    await page.goto(cfg.portal_url)
    result = await inspect_loaded(page, cfg, watch)
    assert result.result == Result.UNKNOWN
    assert result.transient == transient
    assert result.retry_after == 40000


async def test_data_request_failure(page, cfg):
    await page.unroute('**/*')
    await page.route('**/*', lambda r: r.fulfill(status=503) if '/data' in r.request.url else r.fulfill(
        body='<div id="ready">Loaded</div>' + BUTTON.format(attrs='') + '<script>fetch("/data")</script>'))
    watch = NetworkWatch(page, cfg)
    await page.goto(cfg.portal_url)
    assert (await inspect_loaded(page, cfg, watch)).result == Result.UNKNOWN


async def test_login_redirect(page, cfg):
    await page.unroute('**/*')
    await page.route('**/*', lambda r: r.fulfill(status=302, headers={'Location': 'https://login.microsoftonline.com/test'})
                     if 'aims.parking' in r.request.url else r.fulfill(body='<input type="password">'))
    watch = NetworkWatch(page, cfg)
    await page.goto(cfg.portal_url)
    assert (await inspect_loaded(page, cfg, watch)).result == Result.AUTH_REQUIRED


async def test_generic_password_input_is_not_auth(page, cfg):
    await page.set_content('<div id="ready">Loaded</div><input type="password">')
    assert (await inspect_loaded(page, cfg, NetworkWatch(page, cfg))).result == Result.UNKNOWN


async def test_storage_round_trip(browser, cfg):
    cfg = replace(cfg, session_storage=True)
    context = await browser.new_context()
    await context.route('**/*', lambda r: r.fulfill(body='<div>fixture</div>'))
    page = await context.new_page()
    await page.goto('https://fixture.test')
    await page.evaluate('''async () => {
        localStorage.setItem('local', 'yes'); sessionStorage.setItem('session', 'yes');
        document.cookie = 'cookie=yes; Secure';
        await new Promise((resolve, reject) => {
            const request = indexedDB.open('fixture', 1);
            request.onupgradeneeded = () => request.result.createObjectStore('items');
            request.onsuccess = () => { const db = request.result;
                const tx = db.transaction('items', 'readwrite'); tx.objectStore('items').put('yes', 'key');
                tx.oncomplete = () => { db.close(); resolve(); }; tx.onerror = reject; };
        });
    }''')
    bundle = await export_context(context, cfg)
    await context.close()
    restored = await new_context(browser, bundle)
    await restored.route('**/*', lambda r: r.fulfill(body='fixture'))
    page = await restored.new_page()
    await page.goto('https://fixture.test')
    assert await page.evaluate('localStorage.local') == 'yes'
    assert await page.evaluate('sessionStorage.session') == 'yes'
    assert await page.evaluate('document.cookie') == 'cookie=yes'
    assert bundle['storage']['origins'][0]['indexedDB'][0]['name'] == 'fixture'
    await page.goto('https://unrelated.test')
    assert await page.evaluate('sessionStorage.session') is None
    await restored.close()


def test_retry_after():
    assert retry_after('400') == 400
    assert retry_after('garbage') == 0
    assert retry_after(None) == 0
    assert retry_after('Wed, 21 Oct 2015 07:28:00 GMT') == 0
