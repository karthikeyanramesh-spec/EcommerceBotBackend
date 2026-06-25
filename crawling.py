

from scrapling.spiders import Spider, Request, Response


async def ensure_playwright_browsers():
    """Install Playwright browsers if they don't exist"""
    import subprocess
    from pathlib import Path

    browsers_path = Path.home() / ".cache" / "ms-playwright"
    chromium_path = list(browsers_path.glob("chromium-*"))

    if not chromium_path:
        print("Installing Playwright browsers (this may take a minute)...")
        try:
            subprocess.run(
                ["playwright", "install", "chromium", "--with-deps"],
                check=True,
                capture_output=True
            )
            print("✓ Playwright browsers installed successfully!")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to install browsers: {e}")


from scrapling.fetchers import (
    FetcherSession,
    AsyncDynamicSession,
    AsyncStealthySession
)
from playwright.async_api import async_playwright
from urllib.parse import urlparse, urljoin
import asyncio
import re
import os
import certifi

os.environ["SSL_CERT_FILE"] = certifi.where()


class UltraCrawler(Spider):

    name = "ultra_crawler"

    def __init__(self, start_url, max_urls: int = 100000):

        super().__init__()

        self.start_url = start_url
        self.max_urls = max_urls

        self.domain = urlparse(start_url).netloc

        self.visited = set()
        self.queued = set()

        self.semaphore = asyncio.Semaphore(5)
        self.playwright = None
        self.browser = None
        self.browser_context = None

    async def setup_browser(self):
        await ensure_playwright_browsers()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.browser_context = await self.browser.new_context(
            ignore_https_errors=True
        )

    async def close_browser(self):
        if self.browser_context:
            await self.browser_context.close()
            self.browser_context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
    def configure_sessions(self, manager):

        manager.add(
            "fast",
            FetcherSession(
                impersonate="chrome",
                verify=False
            )
        )

        manager.add(
            "dynamic",
            AsyncDynamicSession(
                headless=True,
                wait_until="domcontentloaded",
                additional_args={
                    "ignore_https_errors": True
                }
            
            )
        )

        manager.add(
            "stealth",
            AsyncStealthySession(
                headless=True,
                wait_until="networkidle"
            )
        )

    async def start_requests(self):
        await self.setup_browser()

        self.queued.add(self.start_url)

        yield Request(
            self.start_url,
            sid="fast",
            callback=self.parse
        )

    def clean_url(self, url):

        parsed = urlparse(url)

        if parsed.netloc != self.domain:
            return None

        clean = (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

        clean = clean.split("?")[0]
        clean = clean.split("#")[0]

        blacklist = (
            ".jpg", ".jpeg", ".png",
            ".svg", ".gif", ".webp",
            ".pdf", ".zip", ".css",
            ".js", ".woff", ".woff2",
            ".mp4", ".ico"
        )

        if clean.lower().endswith(blacklist):
            return None

        return clean

    async def extract_links(self, response):

        links = set()

        try:

            hrefs = response.css(
                "a::attr(href)"
            ).getall()

            for href in hrefs:

                links.add(
                    response.urljoin(href)
                )

        except:
            pass

        try:

            html = response.text

            regex_urls = re.findall(
                r'https?://[^\s"\'<>]+',
                html
            )

            links.update(regex_urls)

        except:
            pass

        return links

    async def retry_dynamic(self, url):

        try:

            response = await self.session_manager.get(
                url,
                sid="dynamic"
            )

            return response
            print("dynamicjkdfsfjlkl")

        except:
            return None

    async def retry_stealth(self, url):

        try:

            response = await self.session_manager.get(
                url,
                sid="stealth"
            )

            return response
            print("dfdfdfdf")

        except:
            return None
    async def get_dynamic_links(self, url):
        links = set()
        if not self.browser_context:
            return links

        page = await self.browser_context.new_page()
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000
            )
            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(el => el.href)"
            )
            for href in hrefs:
                links.add(href)
            html = await page.content()
            regex_links = re.findall(r'https?://[^\s"\'>]+', html)
            links.update(regex_links)
        except Exception as e:
            print(f"PLaywright Error: {url} -> {e}")
        finally:
            await page.close()
        return links

    async def parse(self, response: Response):

        async with self.semaphore:
            if self.max_urls and len(self.visited) >= self.max_urls:
                return

            links = set()
            try:
                if response.url not in self.visited:

                    self.visited.add(response.url)

                    print(
                        f"[{len(self.visited)}] "
                        f"{response.url}"
                    )

                    yield {
                        "url": response.url
                    }

                if self.max_urls and len(self.visited) >= self.max_urls:
                    return

                links = await self.extract_links(
                    response
                )
            except Exception as e:
                print(f"Fetcher link extraction failed: {e}")

            # Dynamic fallback
            try:
                if len(links) < 7:

                    dyn = await self.retry_dynamic(
                        response.url
                    )

                    if dyn:

                        links.update(
                            await self.extract_links(dyn)
                        )
            except Exception as e:
                print(f"Dynamic fallback failed: {e}")

            # Anti-bot fallback
            try:
                if len(links) < 5:

                    stealth = await self.retry_stealth(
                        response.url
                    )

                    if stealth:

                        links.update(
                            await self.extract_links(
                                stealth
                            )
                        )
            except Exception as e:
                print(f"Stealth fallback failed: {e}")
            
            try:
                if len(links) < 15:
                    pw_links = await self.get_dynamic_links(response.url)
                    links.update(pw_links)
            except Exception as e:
                print(f"Playwright fallback failed: {e}")

            for link in links:

                clean = self.clean_url(link)

                if not clean:
                    continue

                if clean in self.queued:
                    continue

                self.queued.add(clean)

                yield Request(
                    clean,
                    sid="fast",
                    callback=self.parse
                )

    async def closed(self):
        await self.close_browser()

        print("\n========== SUMMARY ==========")

        print(
            f"Total URLs Found: "
            f"{len(self.visited)}"
        )

