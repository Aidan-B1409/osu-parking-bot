import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from urllib.parse import urlparse

from playwright.async_api import Error, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .storage import atomic_json


class Result(StrEnum):
    AVAILABLE = 'AVAILABLE'
    UNAVAILABLE = 'UNAVAILABLE'
    AUTH_REQUIRED = 'AUTH_REQUIRED'
    UNKNOWN = 'UNKNOWN'


@dataclass
class Observation:
    result: Result
    reason: str
    transient: bool = False
    retry_after: float = 0

    def as_dict(self):
        return asdict(self)


def retry_after(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


async def inspect(page, cfg):
    target = page.locator(f'button#ptypeid_btn_{cfg.permit_id}')
    # Count all matches, including hidden duplicates: ambiguity fails closed.
    if await target.count() != 1:
        return Observation(Result.UNKNOWN, 'Expected exactly one permit button')
    if not await target.is_visible():
        return Observation(Result.UNKNOWN, 'Permit button is hidden')
    if ' '.join((await target.inner_text()).split()) != cfg.permit_label:
        return Observation(Result.UNKNOWN, 'Permit label changed')
    if await target.get_attribute('value') != cfg.permit_id:
        return Observation(Result.UNKNOWN, 'Permit value changed')
    if await target.get_attribute('disabled') is not None:
        return Observation(Result.UNAVAILABLE, 'Permit has disabled attribute')
    if not await target.is_enabled():
        return Observation(Result.UNKNOWN, 'Permit is indirectly disabled')
    return Observation(Result.AVAILABLE, 'Expected permit button is enabled')


async def recognized_auth(page, cfg):
    if urlparse(page.url).hostname in cfg.auth_hosts:
        return True
    return bool(cfg.auth_selector and await page.locator(cfg.auth_selector).count()
                and await page.locator(cfg.auth_selector).first.is_visible())


class NetworkWatch:
    def __init__(self, page, cfg):
        self.errors = []
        self.pending = set()
        self.last_activity = time.monotonic()
        self.cfg = cfg
        page.on('request', self.request)
        page.on('response', self.response)
        page.on('requestfinished', self.finished)
        page.on('requestfailed', self.failed)

    def relevant(self, request):
        return request.resource_type == 'document' or (
            request.resource_type in {'xhr', 'fetch'} and (
                urlparse(request.url).netloc == urlparse(self.cfg.portal_url).netloc
                or any(p in request.url for p in self.cfg.data_request_patterns)))

    def request(self, request):
        if self.relevant(request):
            self.pending.add(request)
            self.last_activity = time.monotonic()

    def response(self, response):
        if self.relevant(response.request) and response.status >= 400:
            status = response.status
            self.errors.append(Observation(Result.UNKNOWN, f'Portal HTTP {status}',
                                           status == 429 or status >= 500,
                                           retry_after(response.headers.get('retry-after'))))

    def finished(self, request):
        if self.relevant(request):
            self.pending.discard(request)
            self.last_activity = time.monotonic()

    def failed(self, request):
        if self.relevant(request):
            self.errors.append(Observation(Result.UNKNOWN, 'Portal request failed', True))
        self.finished(request)

    async def settle(self):
        while self.pending or time.monotonic() - self.last_activity < 1:
            await asyncio.sleep(0.1)

    def failure(self):
        if not self.errors:
            return None
        # Honor the longest server delay if several requests failed.
        error = next((e for e in self.errors if not e.transient), self.errors[0])
        error.retry_after = max(e.retry_after for e in self.errors)
        return error


async def inspect_loaded(page, cfg, watch):
    if await recognized_auth(page, cfg):
        return Observation(Result.AUTH_REQUIRED, 'Recognized sign-in screen')
    if watch.failure():
        return watch.failure()
    if not cfg.ready_selector:
        return Observation(Result.UNKNOWN, 'Readiness selector has not been calibrated')
    try:
        await page.locator(cfg.ready_selector).wait_for(state='visible', timeout=30000)
        await watch.settle()
    except PlaywrightTimeout:
        if await recognized_auth(page, cfg):
            return Observation(Result.AUTH_REQUIRED, 'Recognized sign-in screen')
        return watch.failure() or Observation(Result.UNKNOWN, 'Permit readiness signal missing')
    if await recognized_auth(page, cfg):
        return Observation(Result.AUTH_REQUIRED, 'Recognized sign-in screen')
    return watch.failure() or await inspect(page, cfg)


async def new_context(browser, bundle=None):
    context = await browser.new_context(storage_state=bundle['storage'] if bundle else None)
    if bundle and bundle.get('session_storage'):
        script = ('const saved = ' + json.dumps(bundle['session_storage']) + ';'
                  'if (saved[location.origin]) { for (const [k,v] of '
                  'Object.entries(saved[location.origin])) sessionStorage.setItem(k,v); }')
        await context.add_init_script(script=script)
    return context


async def export_context(context, cfg):
    session = {}
    if cfg.session_storage:
        for page in context.pages:
            for frame in page.frames:
                origin = urlparse(frame.url)
                if origin.scheme in {'https', 'http'}:
                    session[f'{origin.scheme}://{origin.netloc}'] = await frame.evaluate(
                        'Object.fromEntries(Object.entries(sessionStorage))')
    return {'storage': await context.storage_state(indexed_db=True), 'session_storage': session}


async def launch(playwright, cfg, headless=True, env=None):
    return await playwright.chromium.launch(headless=headless, chromium_sandbox=cfg.sandbox,
                                            executable_path=cfg.executable, env=env)


async def check(cfg, bundle=None, save=True):
    if bundle is None:
        if not cfg.session_path.exists():
            return Observation(Result.AUTH_REQUIRED, 'No saved browser session')
        bundle = json.loads(cfg.session_path.read_text())
    try:
        async with asyncio.timeout(cfg.check_timeout):
            async with async_playwright() as playwright:
                browser = await launch(playwright, cfg)
                try:
                    context = await new_context(browser, bundle)
                    page = await context.new_page()
                    watch = NetworkWatch(page, cfg)
                    await page.goto(cfg.portal_url, wait_until='domcontentloaded', timeout=45000)
                    for selector in cfg.navigation:
                        if await recognized_auth(page, cfg) or watch.failure():
                            break
                        await page.locator(selector).click(timeout=15000)
                        await page.wait_for_load_state('domcontentloaded')
                    result = await inspect_loaded(page, cfg, watch)
                    if save and result.result in {Result.AVAILABLE, Result.UNAVAILABLE}:
                        atomic_json(cfg.session_path, await export_context(context, cfg))
                    return result
                finally:
                    await browser.close()
    except (TimeoutError, PlaywrightTimeout):
        return Observation(Result.UNKNOWN, 'Browser check timed out', True)
    except Error:
        return Observation(Result.UNKNOWN, 'Browser operation failed', True)
