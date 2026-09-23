import pytest
from playwright.async_api import async_playwright

from parking_bot.config import Config
from parking_bot.state import State
from parking_bot.storage import prepare


@pytest.fixture
def cfg(tmp_path):
    prepare(tmp_path)
    return Config(data_dir=tmp_path, ready_selector='#ready', channel_id='1234', token_file=tmp_path / 'token')


@pytest.fixture
def state(cfg):
    db = State(cfg)
    yield db
    db.close()


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(chromium_sandbox=True)
        yield browser
        await browser.close()


@pytest.fixture
async def page(browser):
    page = await browser.new_page()
    # Every request is intercepted. No university or Discord traffic in tests.
    await page.route('**/*', lambda route: route.abort())
    yield page
    await page.close()
