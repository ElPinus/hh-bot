import json
import re
import asyncio
import logging
from urllib.parse import urlparse, urlencode, parse_qsl
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from config import HH_LOGIN, HH_COOKIES_PATH, HH_PROXY

logger = logging.getLogger(__name__)

LOGIN_URL = "https://hh.ru/account/login"
SEARCH_URL = "https://hh.ru/search/vacancy"
BASE_URL = "https://hh.ru"

# Navigation (page.goto) timeout. The default Playwright timeout is 30s,
# which a slow-but-working HH proxy regularly overshoots — every overshoot
# surfaces as "Page.goto: Timeout 30000ms exceeded" and the search/apply/
# negotiations op returns empty. Raise it so slow loads finish instead of
# aborting. Applied only to navigation (set_default_navigation_timeout);
# element/locator waits keep the 30s default so genuinely-missing elements
# still fail fast and don't stall the autopilot loop.
NAV_TIMEOUT_MS = 60_000

# Resource types blocked on headless pages to cut bandwidth through the
# (metered, slow) HH proxy. We only need the DOM/text of a vacancy, never
# its visuals — aborting images/media/fonts/stylesheets drops ~80-90% of
# the bytes per page, so loads finish faster and burn far less proxy
# traffic. Scripts and xhr/fetch are kept so hh.ru's dynamic content and
# the apply flow still work. NOT applied to the visible login browser
# (the user needs to see a styled page to log in).
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


def _proxy_config() -> dict | None:
    """Parse HH_PROXY env var into Playwright proxy dict, or None if unset."""
    if not HH_PROXY:
        return None
    parsed = urlparse(HH_PROXY)
    # Playwright wants 'server' as scheme://host:port, credentials separately
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    cfg: dict = {"server": server}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    logger.info("Using HH proxy: %s", server)
    return cfg


def _looks_like_salary(text: str) -> str:
    """Return `text` if it looks like a salary string, otherwise "".

    hh.ru cards sometimes render the experience label ("Опыт более 6
    лет") in a DOM node that loose selectors grab instead of the salary
    cell — that's how some stored vacancies ended up with salary="Опыт
    более 6 лет". This guard rejects such noise: a real salary has
    digits AND a currency marker, and never the word "опыт".
    """
    if not text:
        return ""
    low = text.lower()
    if "опыт" in low or "experience" in low:
        return ""
    if "не указан" in low:  # "з/п не указана"
        return ""
    has_digit = any(ch.isdigit() for ch in text)
    currency_markers = (
        "₽", "руб", "р.", "$", "€", "usd", "eur", "kzt", "тенge",
        "тенге", "₸", "сум", "byn", "uah", "грн",
    )
    has_currency = any(m in low for m in currency_markers)
    return text if (has_digit and has_currency) else ""


class HHClient:
    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self):
        self.playwright = await async_playwright().start()
        proxy = _proxy_config()
        self.browser = await self.playwright.chromium.launch(
            headless=True, proxy=proxy
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        self.page = await self.context.new_page()
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        await self._install_asset_blocking()

        if HH_COOKIES_PATH.exists():
            await self._load_cookies()
            return "session_ok"
        else:
            return "need_login"

    async def _install_asset_blocking(self):
        """Abort image/media/font/stylesheet requests on the current context
        to cut proxy bandwidth (~80-90% per page). See _BLOCKED_RESOURCE_TYPES.
        Routing is set on the context, so it covers current and future pages.
        """
        async def _route(route):
            try:
                # _apply_mode loads the FULL page (CSS/JS/fonts) — hh's Magritte
                # response form won't mount with stylesheets aborted. Search
                # keeps blocking to save proxy bandwidth.
                if (
                    not getattr(self, "_apply_mode", False)
                    and route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                ):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                # Request may already be handled or the page closed mid-flight.
                pass

        await self.context.route("**/*", _route)

    async def stop(self):
        """Close the browser and Playwright. Robust against partial state —
        each step is isolated so a failure in one doesn't leak the other."""
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.warning("stop(): browser.close() failed: %s", type(e).__name__)
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.warning("stop(): playwright.stop() failed: %s", type(e).__name__)

    async def _relaunch_headless(self):
        """Close the current (visible) browser and reopen a headless one with
        the standard context + raised navigation timeout. Used after the
        interactive login completes, times out, or fails to open the login
        page — so the client never stays stuck on the visible login browser
        (which would wedge the autopilot/messages loops that share self.page).
        """
        if self.browser:
            await self.browser.close()
        self.browser = await self.playwright.chromium.launch(
            headless=True, proxy=_proxy_config()
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.page = await self.context.new_page()
        await self._install_asset_blocking()

    async def login_interactive(self) -> str:
        """
        Open a visible browser window for manual login.
        User handles captcha/code themselves, we just save cookies after.
        Returns: 'success', 'error:...'
        """
        # close headless browser temporarily
        if self.browser:
            await self.browser.close()

        # open visible browser via installed Google Chrome.
        # Bundled Chromium fails on some Windows setups with "spawn UNKNOWN"
        # for visible mode; Edge channel ignores HTTP_PROXY on enterprise/DoH.
        # Chrome works for both.
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            channel="chrome",
            proxy=_proxy_config(),
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
        )
        self.page = await self.context.new_page()
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        try:
            await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.error("login_interactive: could not open login page: %s", e)
            await self._relaunch_headless()
            return "error:login_page_unreachable"

        logger.info("Visible browser opened for login. Waiting for user to log in...")

        # poll until user is logged in (check every 3 sec, timeout 5 min)
        for i in range(100):
            await asyncio.sleep(3)
            try:
                url = self.page.url
                logger.debug("Login poll #%d, URL: %s", i, url)
                if await self._is_logged_in():
                    await self._save_cookies()
                    logger.info("Login detected, cookies saved")

                    # switch back to headless
                    await self._relaunch_headless()
                    await self._load_cookies()
                    return "success"
            except Exception as e:
                logger.debug("Login check error: %s", e)

        # timeout
        await self._relaunch_headless()
        return "error:login_timeout_5min"

    async def _save_cookies(self):
        cookies = await self.context.cookies()
        HH_COOKIES_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
        logger.info("Cookies saved")

    async def _load_cookies(self):
        cookies = json.loads(HH_COOKIES_PATH.read_text())
        await self.context.add_cookies(cookies)
        logger.info("Cookies loaded")

        try:
            await self.page.goto(BASE_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            if await self._is_logged_in():
                logger.info("Session valid")
            else:
                logger.info("Session expired, need re-login")
                HH_COOKIES_PATH.unlink(missing_ok=True)
        except Exception as e:
            # Slow proxy: the startup validation navigation can exceed the
            # timeout. A timeout is inconclusive, NOT proof the session is
            # dead — keep the cookies and start anyway. Validity is
            # re-checked on first real use (pasted link / autopilot).
            # Crashing startup over a slow proxy would make the bot
            # un-launchable during slow spells.
            logger.warning(
                "Cookie validation navigation failed (%s) - starting anyway, "
                "will re-check session on first use",
                type(e).__name__,
            )

    async def _is_logged_in(self) -> bool:
        """Check whether the current session is authenticated on hh.ru.

        Primary signal is the `hhtoken` auth cookie — it is page-
        independent, so this works even when the browser is parked on a
        vacancy page (the autopilot keeps it there between cycles).
        The old implementation only checked `document.body.innerText`
        for profile-menu markers, which are absent on vacancy pages —
        that produced constant false "not logged in" in messages_loop.

        Page-content and URL checks remain as fallbacks for the rare
        case where the cookie is present but stale; `_load_cookies()`
        already does a strict navigation-based check at startup and
        drops the cookie file if the session is dead.
        """
        try:
            url = self.page.url
            if "/account/login" in url:
                return False

            # Primary: auth cookie present (page-independent).
            try:
                cookies = await self.context.cookies()
                if any(c.get("name") == "hhtoken" for c in cookies):
                    return True
            except Exception:
                pass  # fall through to content checks

            # Fallback: page content (only meaningful on hh pages that
            # render the profile menu — vacancy pages won't have it).
            result = await self.page.evaluate("""() => {
                const text = document.body?.innerText || '';
                const hasProfile = text.includes('Резюме и профиль')
                    || text.includes('Отклики')
                    || text.includes('Просмотры резюме');
                const hasLoginForm = !!document.querySelector('input[data-qa*="credential-type"]')
                    || !!document.querySelector('input[data-qa*="account-type"]');
                return hasProfile && !hasLoginForm;
            }""")
            if result:
                return True

            # Fallback: URL patterns.
            if any(p in url for p in ["/applicant/", "/resume/", "/dashboard"]):
                return True

            return False
        except Exception:
            return False

    def is_logged_in_sync(self) -> bool:
        """Check if cookies exist (non-async quick check)."""
        return HH_COOKIES_PATH.exists()

    async def login_step1_send_phone(self) -> str:
        """
        Go to login page, enter phone number, click submit.
        Returns: 'code_sent' if SMS code form appeared,
                 'already_logged_in' if session is valid,
                 'error:...' on failure.
        """
        await self.page.goto(BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        if await self._is_logged_in():
            return "already_logged_in"

        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Step 1: select "Applicant" account type and click Next
        applicant_radio = await self.page.query_selector(
            'input[data-qa*="APPLICANT"]'
        )
        if applicant_radio:
            await self.page.evaluate(
                'document.querySelector(\'input[data-qa*="APPLICANT"]\').click()'
            )
            await asyncio.sleep(1)
            await self.page.evaluate(
                'document.querySelector(\'button[data-qa="submit-button"]\').click()'
            )
            await asyncio.sleep(3)
            logger.info("Selected applicant, clicked next")

        # Step 2: ensure phone tab is selected
        phone_radio = await self.page.query_selector(
            'input[data-qa*="credential-type-PHONE"]'
        )
        if phone_radio:
            is_checked = await phone_radio.get_attribute("data-qa")
            if "checked" not in (is_checked or ""):
                await self.page.evaluate(
                    'document.querySelector(\'input[data-qa*="credential-type-PHONE"]\').click()'
                )
                await asyncio.sleep(1)

        # Step 3: find phone number input
        phone_input = await self.page.query_selector(
            'input[data-qa="magritte-phone-input-national-number-input"]'
        )

        if not phone_input:
            # fallback selectors
            phone_input = await self.page.query_selector(
                'input[inputmode="tel"], '
                'input[data-qa="login-input-username"], '
                'input[type="tel"]'
            )

        if not phone_input:
            logger.error("Could not find phone input on login page")
            return "error:phone_input_not_found"

        # strip country code prefix if present (hh.ru has separate field for it)
        phone = HH_LOGIN.strip()
        if phone.startswith("+7"):
            phone = phone[2:]
        elif phone.startswith("8") and len(phone) == 11:
            phone = phone[1:]

        await phone_input.fill(phone)
        await asyncio.sleep(1)

        # click submit / "get code" button
        submit = await self.page.query_selector(
            'button[data-qa="submit-button"]'
        )
        if submit:
            await self.page.evaluate(
                'document.querySelector(\'button[data-qa="submit-button"]\').click()'
            )
        else:
            await phone_input.press("Enter")

        await asyncio.sleep(3)

        # check if code input appeared
        code_input = await self.page.query_selector(
            'input[data-qa="magritte-pincode-input-field"], '
            'input[autocomplete="one-time-code"], '
            'input[inputmode="numeric"]'
        )

        if code_input:
            logger.info("Code input appeared, waiting for user to provide code")
            return "code_sent"

        # maybe already logged in after phone submit
        if await self._is_logged_in():
            await self._save_cookies()
            return "already_logged_in"

        # check for errors
        error_el = await self.page.query_selector(
            '[data-qa="login-error-message"], '
            '.bloko-notification_error'
        )
        if error_el:
            error_text = (await error_el.inner_text()).strip()
            return f"error:{error_text}"

        return "error:unknown_state_after_phone_submit"

    async def login_step2_enter_code(self, code: str) -> str:
        """
        Enter the SMS/push code on the login page.
        hh.ru auto-submits when all digits are entered, causing immediate navigation.
        Returns: 'success', 'wrong_code', 'error:...'
        """
        try:
            code_input = await self.page.query_selector(
                'input[data-qa="magritte-pincode-input-field"], '
                'input[autocomplete="one-time-code"], '
                'input[inputmode="numeric"]'
            )

            if not code_input:
                return "error:code_input_not_found"

            # click to focus first
            await code_input.click()
            await asyncio.sleep(0.5)

            # type code char by char to trigger auto-submit on last digit
            # hh.ru auto-submits and navigates, so wrap everything in try
            await code_input.type(code.strip(), delay=150)
            logger.info("Code typed, waiting for auto-submit navigation")

        except Exception as e:
            err = str(e)
            if "Execution context was destroyed" in err or "navigation" in err.lower():
                logger.info("Page navigated during code entry (auto-submit)")
            else:
                return f"error:{err}"

        # wait for redirect to settle
        await asyncio.sleep(5)

        # navigate to main page to verify login
        try:
            await self.page.goto(BASE_URL, wait_until="domcontentloaded")
        except Exception:
            await asyncio.sleep(3)
            try:
                await self.page.goto(BASE_URL, wait_until="domcontentloaded")
            except Exception:
                pass

        await asyncio.sleep(2)

        if await self._is_logged_in():
            await self._save_cookies()
            logger.info("Login with code successful")
            return "success"

        # maybe still on login page with error
        try:
            error_el = await self.page.query_selector(
                '[data-qa="login-error-message"], '
                '.bloko-notification_error'
            )
            if error_el:
                error_text = (await error_el.inner_text()).strip()
                if "код" in error_text.lower() or "code" in error_text.lower():
                    return "wrong_code"
                return f"error:{error_text}"
        except Exception:
            pass

        return "error:login_not_confirmed"

    async def search_vacancies(
        self, filters: dict, pages: int = 4, per_page: int = 50,
        order_by: str = "publication_time",
    ) -> list[dict]:
        """Search vacancies by filters across multiple pages.

        Defaults: 4 pages × 50/page = up to 200 vacancies per filter per cycle.
        `order_by` defaults to publication_time (freshest first) so the
        autopilot sees newly-posted vacancies before stale ones; pass
        "relevance" for hh's relevance ordering.

        Stops early if any page returns no cards (end of results).
        """
        base_params: list[tuple[str, str]] = [
            ("text", filters.get("keywords", "")),
            ("order_by", order_by),
        ]
        if filters.get("city"):
            area = await self._resolve_area(filters["city"])
            if area:
                base_params.append(("area", area))
        if filters.get("salary_from"):
            base_params.append(("salary", str(filters["salary_from"])))
            base_params.append(("only_with_salary", "true"))
        if filters.get("experience"):
            base_params.append(("experience", filters["experience"]))
        if filters.get("schedule"):
            base_params.append(("schedule", filters["schedule"]))
        # Restrict search to the vacancy title when requested (search_field=name)
        # so broad queries like "AI"/"ИИ" match titles, not description mentions.
        if filters.get("search_field"):
            base_params.append(("search_field", filters["search_field"]))

        return await self._paginate_search(SEARCH_URL, base_params, pages, per_page)

    async def search_by_url(
        self, base_url: str, pages: int = 4, per_page: int = 50,
        order_by: str = "publication_time",
    ) -> list[dict]:
        """Search vacancies from an arbitrary hh.ru search URL.

        Built for the résumé-based "similar vacancies" search
        (`/search/vacancy?resume=<hash>`): hh matches against the WHOLE
        résumé instead of a single keyword, which is far less noisy than
        the keyword filters. Whatever query params are baked into
        `base_url` (work_format, salary, area, multi-valued search_field, ...)
        are preserved; `order_by` (default publication_time = freshest first)
        is set/overridden here, and `per_page`/`page` are managed by
        pagination.

        Requires a logged-in session — a résumé search is account-bound.
        """
        parsed = urlparse(base_url)
        # parse_qsl returns a LIST of pairs (not a dict) — this preserves
        # REPEATED keys. The résumé search uses search_field=name&
        # search_field=company_name&search_field=description; dict() would
        # collapse those to just the last one and silently narrow the search.
        pairs = parse_qsl(parsed.query, keep_blank_values=False)
        # ordering + pagination are ours — drop any baked-in copies so they
        # don't end up duplicated, then set order_by (freshest first).
        base_params = [
            (k, v) for (k, v) in pairs
            if k not in ("page", "per_page", "order_by")
        ]
        base_params.append(("order_by", order_by))
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or "hh.ru"
        path = parsed.path or "/search/vacancy"
        search_base = f"{scheme}://{netloc}{path}"
        return await self._paginate_search(search_base, base_params, pages, per_page)

    async def _paginate_search(
        self, search_base: str, base_params: list, pages: int, per_page: int
    ) -> list[dict]:
        """Paginate an hh.ru vacancy SERP and collect parsed cards.

        `search_base` is scheme://host/path (no query); `base_params` is a
        LIST of (key, value) pairs (repeated keys allowed — e.g. multi-valued
        search_field), WITHOUT page/per_page (set here). Iterates
        page=0..pages-1, parses each card via `_parse_card`, dedups by id,
        and stops early when a page yields no cards or no NEW cards (end of
        results / repeated page). Shared by `search_vacancies` (filter-
        driven) and `search_by_url` (résumé / raw-URL driven).
        """
        base = [(k, v) for (k, v) in base_params if k not in ("page", "per_page")]
        base.append(("per_page", str(per_page)))
        vacancies: list[dict] = []
        seen_ids: set[str] = set()

        page_idx = 0
        for page_idx in range(pages):
            params = base + [("page", str(page_idx))]
            url = f"{search_base}?{urlencode(params)}"
            logger.info("Searching (page %d): %s", page_idx, url)

            try:
                await self.page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning("goto failed on page %d: %s", page_idx, e)
                break
            # Employer names are JS-hydrated AFTER domcontentloaded: the
            # employer element exists immediately but EMPTY, then its text is
            # filled in. A fixed sleep raced this on the (slow) proxy, so the
            # card parse grabbed an empty company most of the time — which
            # silently disabled the company blacklist and cross-post dedup.
            # Wait until the employer text is actually populated on most cards.
            try:
                await self.page.wait_for_function(
                    """() => {
                        const els = document.querySelectorAll(
                            '[data-qa="vacancy-serp__vacancy-employer"]');
                        if (!els.length) return false;
                        let filled = 0;
                        els.forEach(e => {
                            if ((e.textContent || '').trim()) filled++;
                        });
                        return filled >= Math.max(1, Math.floor(els.length * 0.6));
                    }""",
                    timeout=8000,
                )
            except Exception:
                pass
            await asyncio.sleep(1.0)

            cards = await self.page.query_selector_all(
                '[data-qa="vacancy-serp__vacancy"], '
                '[class*="vacancy-search-item"], '
                '.serp-item'
            )
            if not cards:
                logger.info("Page %d: no cards, stopping pagination", page_idx)
                break

            new_on_page = 0
            for card in cards:
                try:
                    vacancy = await self._parse_card(card)
                    if vacancy and vacancy["id"] not in seen_ids:
                        seen_ids.add(vacancy["id"])
                        vacancies.append(vacancy)
                        new_on_page += 1
                except Exception as e:
                    logger.warning("Failed to parse card: %s", e)

            logger.info("Page %d: parsed %d cards (%d new total)",
                        page_idx, new_on_page, len(vacancies))
            if new_on_page == 0:
                # all cards on this page already seen — likely duplicate page
                break

        logger.info("Search complete: %d unique vacancies across %d page(s)",
                    len(vacancies), page_idx + 1)
        return vacancies

    async def _parse_card(self, card) -> dict | None:
        title_el = await card.query_selector(
            '[data-qa="serp-item__title"], '
            '[data-qa="vacancy-serp__vacancy-title"], '
            'a[class*="title"]'
        )
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        url = await title_el.get_attribute("href")
        if not url:
            return None

        vacancy_id = ""
        if "/vacancy/" in url:
            vacancy_id = url.split("/vacancy/")[1].split("?")[0].split("/")[0]
        if not vacancy_id:
            return None

        # Employer name. The data-qa moved to ...-employer-text on the
        # current SERP; the old `[class*="company-name"]` matches a now-empty
        # node. Try the text node first, then the container/link, and fall
        # back to text_content (inner_text can return "" with stylesheets
        # blocked). _paginate_search already waits for this to hydrate.
        company = ""
        for sel in (
            '[data-qa="vacancy-serp__vacancy-employer-text"]',
            '[data-qa="vacancy-serp__vacancy-employer"]',
            'a[href*="/employer/"]',
        ):
            company_el = await card.query_selector(sel)
            if not company_el:
                continue
            company = (await company_el.inner_text()).strip() or (
                await company_el.text_content() or ""
            ).strip()
            if company:
                break

        # Only the exact data-qa selector — the loose `[class*="compensation"]`
        # fallback used to grab the experience-label container instead.
        salary_el = await card.query_selector(
            '[data-qa="vacancy-serp__vacancy-compensation"]'
        )
        salary_raw = (await salary_el.inner_text()).strip() if salary_el else ""
        salary = _looks_like_salary(salary_raw)

        city_el = await card.query_selector(
            '[data-qa="vacancy-serp__vacancy-address"], '
            '[class*="area"]'
        )
        city = (await city_el.inner_text()).strip() if city_el else ""

        # Parse work format labels (remote/office/hybrid)
        work_format = ""
        label_els = await card.query_selector_all(
            '[data-qa="vacancy-label-remote"], '
            '[class*="remote-work"], '
            '[class*="work-schedule"], '
            '[class*="label"]'
        )
        labels_text = []
        for lel in label_els:
            txt = (await lel.inner_text()).strip()
            if txt:
                labels_text.append(txt)
        work_format = ", ".join(labels_text)

        return {
            "id": vacancy_id,
            "title": title,
            "company": company,
            "salary": salary,
            "city": city,
            "work_format": work_format,
            "url": url if url.startswith("http") else f"{BASE_URL}{url}",
        }

    async def get_vacancy_description(self, url: str) -> str:
        """Open vacancy page and extract full description."""
        await self.page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        desc_el = await self.page.query_selector(
            '[data-qa="vacancy-description"], '
            '.vacancy-description'
        )
        if desc_el:
            return (await desc_el.inner_text()).strip()
        return ""

    @staticmethod
    def extract_vacancy_id(url: str) -> str | None:
        """Pull the numeric vacancy id out of an hh.ru URL.

        Handles:
          https://hh.ru/vacancy/123456789
          https://spb.hh.ru/vacancy/123456789?query=...
          hh.ru/vacancy/123456789/
        Returns None if the URL doesn't look like a vacancy link.
        """
        m = re.search(r"hh\.ru/vacancy/(\d+)", url)
        return m.group(1) if m else None

    async def get_vacancy_by_url(self, url: str) -> dict | None:
        """Open a single vacancy page and parse it into a vacancy dict.

        Same shape as the dicts produced by `_parse_card` (id, title,
        company, salary, city, url) plus `description`. Used by the
        Telegram "apply by link" flow where the user pastes a vacancy
        URL directly instead of going through search.

        Returns None if the URL is not a valid vacancy link or the page
        could not be parsed (e.g. anti-bot challenge, deleted vacancy).
        """
        vacancy_id = self.extract_vacancy_id(url)
        if not vacancy_id:
            return None

        # Normalise to the canonical URL (drop tracking query params,
        # regional subdomains) — keeps DB ids consistent with autopilot.
        canonical_url = f"{BASE_URL}/vacancy/{vacancy_id}"

        try:
            await self.page.goto(canonical_url, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("get_vacancy_by_url: goto failed for %s: %s",
                           canonical_url, type(e).__name__)
            return None
        await asyncio.sleep(2)

        # Anti-bot guard: a real vacancy page has a substantial body.
        body_text = await self.page.inner_text("body")
        if len(body_text) < 500:
            logger.warning(
                "get_vacancy_by_url: short body (%d chars) for %s — "
                "likely anti-bot challenge", len(body_text), vacancy_id,
            )
            return None

        async def _text(selectors: str) -> str:
            el = await self.page.query_selector(selectors)
            return (await el.inner_text()).strip() if el else ""

        title = await _text(
            '[data-qa="vacancy-title"], h1[class*="title"]'
        )
        if not title:
            logger.warning("get_vacancy_by_url: no title for %s", vacancy_id)
            return None

        company = await _text(
            '[data-qa="vacancy-company-name"], '
            '[data-qa="bloko-header-2"], '
            'a[class*="vacancy-company-name"]'
        )
        salary_raw = await _text(
            '[data-qa="vacancy-salary"], '
            '[data-qa="vacancy-salary-compensation-type-net"], '
            '[data-qa="vacancy-salary-compensation-type-gross"]'
        )
        salary = _looks_like_salary(salary_raw)
        city = await _text(
            '[data-qa="vacancy-view-raw-address"], '
            '[data-qa="vacancy-view-location"], '
            'p[data-qa="vacancy-view-location"]'
        )

        desc_el = await self.page.query_selector(
            '[data-qa="vacancy-description"], .vacancy-description'
        )
        description = (await desc_el.inner_text()).strip() if desc_el else ""

        return {
            "id": vacancy_id,
            "title": title,
            "company": company,
            "salary": salary,
            "city": city,
            "work_format": "",
            "url": canonical_url,
            "description": description,
        }

    async def get_company_rating(self) -> float | None:
        """Try to extract employer rating from the currently loaded vacancy page.

        hh.ru shows ratings like 4.2 / 5 next to the employer name. Selectors
        vary across UI versions, so we try several and fall back to regex
        over the page text.

        Returns None when no rating widget is found (new / small company).
        """

        # 1) try common data-qa selectors
        for sel in [
            '[data-qa="employer-review-small-widget-total-rating"]',
            '[data-qa*="employer-rating"]',
            '[data-qa*="employer-review-rating"]',
            '[data-qa*="reviews-widget-rating"]',
            'div[class*="employer-review-rating"]',
            'div[class*="employer-rating"]',
            'span[class*="rating-value"]',
        ]:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    m = re.search(r"(\d+[.,]\d+)", text)
                    if m:
                        value = float(m.group(1).replace(",", "."))
                        if 0 < value <= 5:
                            return value
            except Exception:
                continue

        # 2) text fallback — look for "Рейтинг 4.2" / "4.2 из 5" near employer block
        try:
            body_text = await self.page.inner_text("body")
            # only search the top of the page (employer block lives near vacancy header)
            head = body_text[:5000]
            patterns = [
                r"рейтинг компании[:\s]+(\d[.,]\d)",
                r"рейтинг работодателя[:\s]+(\d[.,]\d)",
                r"(\d[.,]\d)\s*из\s*5",
                r"оценка компании[:\s]+(\d[.,]\d)",
            ]
            for p in patterns:
                m = re.search(p, head, flags=re.IGNORECASE)
                if m:
                    value = float(m.group(1).replace(",", "."))
                    if 0 < value <= 5:
                        return value
        except Exception:
            pass

        return None

    async def get_employer_id_from_vacancy_page(self) -> str | None:
        """Extract employer id from the link on currently loaded vacancy page."""
        try:
            link = await self.page.query_selector('a[data-qa="vacancy-company-name"]')
            if not link:
                link = await self.page.query_selector('a[href*="/employer/"]')
            if not link:
                return None
            href = await link.get_attribute("href")
            if not href or "/employer/" not in href:
                return None
            return href.split("/employer/")[1].split("?")[0].split("/")[0].strip()
        except Exception:
            return None

    async def fetch_employer_rating(self, employer_id: str) -> float | None:
        """Open /employer/{id} and try to extract rating.

        Navigates away from the current vacancy page — call this only after
        you're done with the vacancy. Returns None if no rating widget
        present on the company page.
        """

        url = f"{BASE_URL}/employer/{employer_id}"
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("Could not open employer %s: %s", employer_id, e)
            return None
        await asyncio.sleep(2)

        for sel in [
            '[data-qa="employer-review-small-widget-total-rating"]',
            '[data-qa*="employer-rating"]',
            '[data-qa*="employer-review-rating"]',
            '[data-qa*="reviews-widget-total-rating"]',
            '[data-qa*="rating-value"]',
            'div[class*="employer-review-rating"]',
            'span[class*="rating-value"]',
        ]:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    m = re.search(r"(\d+[.,]\d+)", text)
                    if m:
                        value = float(m.group(1).replace(",", "."))
                        if 0 < value <= 5:
                            return value
            except Exception:
                continue

        # Text fallback for cases without dedicated data-qa
        try:
            body = await self.page.inner_text("body")
            head = body[:8000]
            for p in [
                r"рейтинг.*?(\d[.,]\d).*?из\s*5",
                r"(\d[.,]\d)\s*из\s*5",
                r"оценка работодателя[:\s]+(\d[.,]\d)",
                r"оценка компании[:\s]+(\d[.,]\d)",
            ]:
                m = re.search(p, head, flags=re.IGNORECASE)
                if m:
                    value = float(m.group(1).replace(",", "."))
                    if 0 < value <= 5:
                        return value
        except Exception:
            pass
        return None

    async def check_remote_available(self, url: str = None) -> bool:
        """Check if current vacancy page has remote work option.

        hh.ru now publishes most vacancies with a
        compound work-format line like "Работа на месте работодателя,
        удалённо или гибрид" — listing three options at once. The old
        marker set ("можно удалённо" / "удалённая работа" / etc.) did
        NOT match that phrasing and rejected such vacancies as
        office-only. The expanded set below catches modern compound
        phrases, plus delimited-context "удалённо" / "гибрид" tokens.
        """
        page_text = await self.page.inner_text("body")
        page_lower = page_text.lower()

        # Tokens that indicate remote is at least an OPTION for the role.
        remote_markers = [
            # Modern hh.ru compound work-format phrasings
            "удалённо или гибрид", "удаленно или гибрид",
            "удалённо или в офис", "удаленно или в офис",
            "гибрид или удалённо", "гибрид или удаленно",
            "офис, гибрид, удалённо", "офис, гибрид, удаленно",
            "удалённо, гибрид", "удаленно, гибрид",
            "удалённо/гибрид", "удаленно/гибрид",
            # Classic markers (older hh pages still use them)
            "можно удалённо", "можно удаленно",
            "удалённая работа", "удаленная работа",
            "полностью удалённо", "полностью удаленно",
            # Explicit "work format" / "schedule" labels
            "формат работы: удалённо", "формат работы: удаленно",
            "формат работы: гибрид",
            "график: удалённый", "график: удаленный",
            # Delimited "удалённо" / "гибрид" — when surrounded by
            # punctuation it almost always means remote is offered.
            ", удалённо", ", удаленно",
            "(удалённо)", "(удаленно)",
            " удалённо.", " удаленно.",
            " удалённо;", " удаленно;",
            "/ удалённо", "/ удаленно",
            ", гибрид", "(гибрид)", " гибрид;",
            # English markers (some hh listings are bilingual)
            "remote work", "remote-friendly", "remote-first",
            "fully remote", "hybrid or remote", "remote or hybrid",
        ]

        # Strong office-only signals. Used only when no remote marker
        # was found — if BOTH kinds appear (multi-option posting),
        # treat as remote-available.
        office_only_markers = [
            "только работа в офисе", "только офис", "только on-site",
            "только on site", "работа только в офисе",
            "присутствие в офисе обязательно",
            "on-site only", "office only",
        ]

        has_remote = any(m in page_lower for m in remote_markers)
        if has_remote:
            return True
        # If we got a suspiciously short body (< 500 chars), the page is
        # most likely an anti-bot challenge / captcha gate, not the real
        # vacancy. Log a warning so this is visible — otherwise it shows
        # up only as a stream of false "no remote" skips.
        if len(page_lower) < 500:
            logger.warning(
                "remote-check: short body (%d chars) — likely anti-bot "
                "challenge from hh.ru. text starts: %r",
                len(page_lower), page_lower[:120],
            )
        is_office_only = any(m in page_lower for m in office_only_markers)
        if is_office_only:
            return False
        # Neither marker matched — vacancy work-format is unclear from
        # page text. Conservative default: skip (False). hh.ru search
        # already filtered with `schedule=remote`, so genuinely-remote
        # vacancies should carry an explicit marker; if they don't, we
        # err on the side of saving tokens rather than wasting an
        # analyzer call on an ambiguous page.
        return False

    async def _dismiss_popups(self):
        """Dismiss subscription / similar-vacancies popups that may cover the
        response form. Deliberately does NOT click generic "Закрыть" /
        bloko-modal-close — those also match the response modal's own close
        button and would shut the apply form."""
        for sel in [
            'button:has-text("Не сейчас")',
            '[data-qa="vacancy-response-similar-vacancies-close"]',
        ]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.5)
                    logger.info("Dismissed popup via %s", sel)
            except Exception:
                pass

    async def _select_resume(
        self, resume_hash: str | None, resume_title: str | None = None
    ) -> bool:
        """Pick the routed résumé in the open hh response modal and CONFIRM it.

        hh's Magritte modal shows the selected résumé as a clickable card
        (data-qa="resume-title"); clicking it expands an inline list where each
        résumé OPTION card carries data-magritte-select-option="<resume hash>"
        plus a data-qa="resume-title" child. Picking one collapses the list back
        to the chosen title.

        Matches by HASH first — the resume= hash from the variant's feed URL,
        which is immune to the user renaming a résumé's title on hh — and falls
        back to the visible title. Verification compares the collapsed header
        against the chosen option's OWN live title, so it holds even if
        feeds.yaml's title has drifted.

        Returns True ONLY when hh confirms the chosen résumé is selected;
        returns False — never raises — when the picker is absent or nothing
        matches, so the caller can refuse to send with the wrong (default)
        résumé.
        """
        if not resume_hash and not resume_title:
            return False

        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "")).strip().lower()

        title_target = _norm(resume_title)
        label = resume_hash or resume_title

        async def _selected() -> str:
            el = await self.page.query_selector('[data-qa="resume-title"]')
            if not el:
                return ""
            try:
                return _norm(await el.inner_text())
            except Exception:
                return ""

        async def _card_title(card) -> str:
            try:
                t = await card.query_selector('[data-qa="resume-title"]')
                return _norm(await (t or card).inner_text())
            except Exception:
                return ""

        try:
            # hh's default may already be the routed résumé. Title-only check
            # (the collapsed header carries no hash); harmless if the title has
            # drifted - we just fall through and select by hash below.
            if title_target and await _selected() == title_target:
                logger.info("Apply: résumé already selected (%s)", label)
                return True

            header = await self.page.query_selector('[data-qa="resume-title"]')
            if not header:
                logger.warning(
                    "Apply: no résumé picker in the modal - cannot select %s", label
                )
                return False

            # Expand the inline résumé list; wait for the option cards to mount.
            await header.click()
            cards = []
            for _ in range(8):  # ~4s
                await asyncio.sleep(0.5)
                cards = await self.page.query_selector_all(
                    '[data-magritte-select-option]'
                )
                if cards:
                    break

            chosen = None
            # Primary: exact résumé hash on the option card (rename-proof).
            if resume_hash:
                chosen = await self.page.query_selector(
                    f'[data-magritte-select-option="{resume_hash}"]'
                )
            # Fallback 1: title match among the hash-bearing option cards.
            if chosen is None and title_target and cards:
                for c in cards:
                    ct = await _card_title(c)
                    if ct and (ct == title_target or title_target in ct or ct in title_target):
                        chosen = c
                        break
            # Fallback 2: older layout without select-option attrs - match the
            # résumé-title cards directly.
            if chosen is None and title_target and not cards:
                for c in await self.page.query_selector_all('[data-qa="resume-title"]'):
                    ct = _norm(await c.inner_text())
                    if ct and (ct == title_target or title_target in ct or ct in title_target):
                        chosen = c
                        break

            if chosen is None:
                avail = []
                for c in cards:
                    h = await c.get_attribute("data-magritte-select-option")
                    avail.append((h, await _card_title(c)))
                logger.warning(
                    "Apply: résumé %s not in picker. Available: %s", label, avail
                )
                return False

            # The chosen option's OWN live title is the expected post-select
            # header text (rename-proof verification).
            expected = await _card_title(chosen)
            await chosen.click()
            for _ in range(8):  # ~4s
                await asyncio.sleep(0.5)
                if expected and await _selected() == expected:
                    logger.info("Apply: selected résumé %s (%r)", label, expected)
                    return True

            logger.warning(
                "Apply: clicked résumé %s but hh shows %r selected - not confirmed",
                label, await _selected(),
            )
            return False
        except Exception as e:
            logger.warning("Apply: résumé selection failed (%s)", e)
            return False

    async def apply_to_vacancy(
        self, vacancy_id: str, cover_letter: str = "",
        resume_identifier: str | None = None, resume_hash: str | None = None,
    ) -> bool:
        """Apply to a vacancy. Loads the page with FULL assets (CSS/JS/fonts):
        hh renders the response form (letter textarea) client-side via its
        Magritte components, which DON'T mount when stylesheets are aborted.
        Asset-blocking stays on for search (bandwidth); only apply needs the
        full page — it's rare and user-triggered, so the extra bytes are fine.

        `resume_hash` (preferred — the resume= hash from the variant's feed URL)
        and `resume_identifier` (visible-title fallback) select the routed
        résumé in the response modal's picker. When either is set, the apply
        FAILS CLOSED if that résumé can't be confirmed selected — it never sends
        with hh's default. Both None leaves hh's default (legacy single-résumé /
        unconfigured-feeds path).
        """
        self._apply_mode = True
        try:
            return await self._apply_to_vacancy_impl(
                vacancy_id, cover_letter, resume_identifier, resume_hash,
            )
        finally:
            self._apply_mode = False

    async def _apply_to_vacancy_impl(
        self, vacancy_id: str, cover_letter: str = "",
        resume_identifier: str | None = None, resume_hash: str | None = None,
    ) -> bool:
        """Apply to a vacancy with cover letter. Returns True if successful."""
        url = f"{BASE_URL}/vacancy/{vacancy_id}"
        # The HH proxy occasionally stalls a navigation past the timeout. A
        # raised TimeoutError here used to escape apply_to_vacancy (documented
        # to return a bool), bubble through the Telegram "Send" handler past
        # its failure branch, and leave the user staring at a stuck
        # "Отправляю отклик..." with no result. Retry once with a longer
        # timeout, then report failure so the caller tells the user to retry
        # or apply manually — never a silent hang.
        for attempt in range(2):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                break
            except Exception as e:
                logger.warning(
                    "apply_to_vacancy: navigation to %s failed (attempt %d/2): %s",
                    vacancy_id, attempt + 1, e,
                )
                if attempt == 1:
                    return False
                await asyncio.sleep(3)
        await asyncio.sleep(3)

        respond_btn = await self.page.query_selector(
            '[data-qa="vacancy-response-link-top"], '
            '[data-qa="vacancy-response-link-bottom"]'
        )
        if not respond_btn:
            logger.warning("No apply button found for %s", vacancy_id)
            return False

        await respond_btn.click()
        await asyncio.sleep(3)

        # Step 1: handle relocation warning if present
        relocation_btn = await self.page.query_selector(
            '[data-qa="relocation-warning-confirm"]'
        )
        if relocation_btn and await relocation_btn.is_visible():
            await relocation_btn.click()
            await asyncio.sleep(2)
            logger.info("Confirmed relocation warning for %s", vacancy_id)

        # Step 1.5: pick the routed résumé in the modal's résumé selector
        # (before filling the letter — switching résumé can reset the form).
        # FAIL-CLOSED: if the routed résumé can't be confirmed selected, ABORT
        # rather than silently send with hh's default. Sending the wrong résumé
        # defeats the whole point of multi-résumé routing. Better a reported
        # failure the user can retry than a wrong-résumé send.
        if resume_hash or resume_identifier:
            if not await self._select_resume(resume_hash, resume_identifier):
                logger.warning(
                    "Apply ABORTED for %s: résumé %s not selected - refusing to "
                    "apply with the wrong résumé.",
                    vacancy_id, resume_hash or resume_identifier,
                )
                return False

        # Helper: poll for the letter textarea, revealing it via the
        # "Сопроводительное" toggle if hidden.
        async def _find_letter_area(seconds: int):
            for _ in range(seconds):
                area = await self.page.query_selector(
                    'textarea[data-qa="vacancy-response-popup-form-letter-input"], '
                    'textarea[name="letter"], '
                    'textarea[name="text"], '
                    'textarea[data-qa*="letter"], '
                    'textarea[placeholder*="опроводитель"], '
                    'textarea'
                )
                if area and await area.is_visible():
                    return area
                toggle = await self.page.query_selector(
                    '[data-qa="vacancy-response-letter-toggle"], '
                    'button:has-text("Сопроводительное"), '
                    'a:has-text("Сопроводительное")'
                )
                if toggle and await toggle.is_visible():
                    await toggle.click()
                await asyncio.sleep(1)
            return None

        # Step 2: find the letter textarea. CRITICAL ordering — look for the
        # form FIRST (the response modal opens right after the click). Only if
        # it's missing do we dismiss a possibly-covering popup, then look again.
        # Dismissing BEFORE finding the form closed the response modal itself
        # (its close button matches generic "Закрыть").
        letter_area = None
        if cover_letter:
            letter_area = await _find_letter_area(10)
            if not letter_area:
                await self._dismiss_popups()
                await asyncio.sleep(1)
                letter_area = await _find_letter_area(6)

            if letter_area:
                try:
                    await letter_area.fill(cover_letter)
                except Exception:
                    # contenteditable / non-fillable — type instead
                    await letter_area.click()
                    await self.page.keyboard.type(cover_letter, delay=10)
                await asyncio.sleep(1)
                logger.info("Cover letter filled for %s", vacancy_id)
            else:
                logger.warning("Could not find letter textarea for %s, applying without letter", vacancy_id)

        # Step 4: find submit button — try data-qa first, then text
        submit_btn = None
        for sel in [
            '[data-qa="vacancy-response-submit-popup"]',
            '[data-qa="vacancy-response-letter-submit"]',
            '[data-qa="vacancy-response-submit"]',
            'button[data-qa*="response-submit"]',
            'button[type="submit"]:has-text("Откликнуться")',
            'button[type="submit"]:has-text("Отправить")',
            'button:has-text("Откликнуться"):visible',
            'button[type="submit"]',
        ]:
            try:
                submit_btn = await self.page.query_selector(sel)
                if submit_btn and await submit_btn.is_visible():
                    break
                submit_btn = None
            except Exception:
                submit_btn = None

        if submit_btn:
            try:
                await submit_btn.click()
            except Exception:
                # might be outside viewport
                await submit_btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)
                await submit_btn.click()
            await asyncio.sleep(4)
            # Clicking a "submit" button is NOT proof the response was sent.
            # The fallback selector matches any submit button on the page, a
            # slow proxy can land the click before the form is ready, and hh
            # may require an extra step (questions, confirmation) — all leave
            # the UI looking done while hh recorded nothing. Verify against hh
            # before claiming success; on doubt report failure (user applies
            # manually) rather than a false "sent".
            if await self._verify_response_sent(vacancy_id):
                logger.info("Applied to vacancy %s (confirmed on hh)", vacancy_id)
                return True
            logger.warning(
                "Submit clicked for %s but hh shows no confirmed response — "
                "reporting FAILURE (apply manually).", vacancy_id,
            )
            return False

        logger.warning("Could not submit application for %s", vacancy_id)
        return False

    async def _verify_response_sent(self, vacancy_id: str) -> bool:
        """Reload the vacancy and confirm hh actually recorded the response.

        After a real apply hh replaces the "Откликнуться" button with a
        "Вы откликнулись" state / a link to the response chat. We read that
        authoritative post-apply state instead of trusting the submit click.
        Returns False on any doubt (favours a false-negative — user re-applies
        manually — over a false-positive that claims a non-existent apply).
        """
        try:
            await self.page.goto(
                f"{BASE_URL}/vacancy/{vacancy_id}", wait_until="domcontentloaded"
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("Verify-response nav failed for %s: %s", vacancy_id, e)
            return False

        # Strongest signal: link to the created response/chat.
        try:
            view = await self.page.query_selector(
                '[data-qa="vacancy-response-link-view-topic"], '
                '[data-qa="vacancy-response-link-view"]'
            )
            if view and await view.is_visible():
                return True
        except Exception:
            pass

        # Fallback: page text markers of an already-sent response.
        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        markers = (
            "вы откликнулись",
            "вы уже откликались",
            "резюме доставлено",
            "отклик доставлен",
        )
        return any(m in body for m in markers)

    async def get_unread_messages(self) -> list[dict]:
        """Open /applicant/negotiations and return list of unread chat threads.

        Returns dicts with keys:
            chat_url, vacancy_title, employer, preview, vacancy_id
        Caller is responsible for browser lock — this method uses self.page.
        """
        url = f"{BASE_URL}/applicant/negotiations"
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("Could not open negotiations page: %s", e)
            return []
        await asyncio.sleep(3)

        # hh.ru negotiation cards. Selectors are best-effort across UI versions.
        items = await self.page.query_selector_all(
            '[data-qa="topic"], '
            '[data-qa="chatik-topic-item"], '
            'div[class*="negotiation-item"], '
            'a[href*="/applicant/negotiations/item"]'
        )

        results: list[dict] = []
        for item in items:
            try:
                # Mark unread by presence of badge/counter or "unread" class.
                unread_marker = await item.query_selector(
                    '[data-qa*="unread"], '
                    '[class*="unread"], '
                    '[class*="new-messages"], '
                    '[class*="counter"]:not([class*="zero"])'
                )
                if not unread_marker:
                    # second pass: bold/highlighted title can also indicate unread
                    bold = await item.query_selector(
                        '[class*="bold"], strong, b'
                    )
                    if not bold:
                        continue

                # Link to the chat (used for dedup + Telegram link)
                link_el = await item.query_selector(
                    'a[href*="negotiations"], a[href*="/chat/"]'
                )
                chat_url = ""
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        chat_url = href if href.startswith("http") else f"{BASE_URL}{href}"

                # Vacancy id from URL if present
                vacancy_id = ""
                if chat_url and "vacancyId=" in chat_url:
                    vacancy_id = chat_url.split("vacancyId=")[1].split("&")[0]

                # Title (vacancy name)
                title_el = await item.query_selector(
                    '[data-qa*="title"], '
                    '[data-qa*="vacancy-name"], '
                    '[class*="vacancy-name"], '
                    '[class*="title"]'
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                # Employer
                employer_el = await item.query_selector(
                    '[data-qa*="employer"], '
                    '[data-qa*="company"], '
                    '[class*="company-name"], '
                    '[class*="employer"]'
                )
                employer = (await employer_el.inner_text()).strip() if employer_el else ""

                # Last message preview
                preview_el = await item.query_selector(
                    '[data-qa*="last-message"], '
                    '[class*="message-preview"], '
                    '[class*="last-message"], '
                    '[class*="snippet"]'
                )
                preview = (await preview_el.inner_text()).strip() if preview_el else ""

                # Fallback — take inner text of the whole card, trimmed
                if not preview:
                    raw = (await item.inner_text()).strip()
                    preview = " ".join(raw.split())[:400]

                if not chat_url and not title and not preview:
                    continue

                results.append({
                    "chat_url": chat_url,
                    "vacancy_id": vacancy_id,
                    "vacancy_title": title,
                    "employer": employer,
                    "preview": preview,
                })
            except Exception as e:
                logger.debug("Negotiations card parse error: %s", e)

        logger.info("Negotiations: %d unread thread(s)", len(results))
        return results

    async def _resolve_area(self, city: str) -> str | None:
        """Resolve city name to hh.ru area id."""
        city_map = {
            "москва": "1",
            "санкт-петербург": "2",
            "новосибирск": "4",
            "екатеринбург": "3",
            "казань": "88",
            "нижний новгород": "66",
            "самара": "78",
            "челябинск": "104",
            "омск": "68",
            "ростов-на-дону": "76",
            "уфа": "99",
            "красноярск": "54",
            "воронеж": "26",
            "пермь": "72",
            "волгоград": "24",
        }
        return city_map.get(city.lower().strip())
