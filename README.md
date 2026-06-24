[Русский](./README.ru.md) · **English**

# hh-bot

A Telegram bot that automates job search and applications on hh.ru with
LLM-based relevance scoring and auto-generated cover letters.

## What it does

- **Search** vacancies via configurable filters using browser automation
  (Playwright).
- **Pre-filter** by an optional title blacklist (empty by default — you add
  title fragments of role types that are off-target *for you*), skipping
  obvious mismatches before spending LLM tokens.
- **Score** each vacancy with an LLM — DeepSeek (primary) with a Groq
  fallback out of the box; both speak the OpenAI-compatible Chat Completions
  API, so swapping in another provider is a small edit in `ai/llm_client.py`.
  The analyzer prompt is generic — it scores fit against the candidate profile
  from `prompts/analyzer_summary.txt` and ships with an inline guide on how to
  tighten scoring (role-type rules, anti-domains, base scores) for your
  own profile.
- **Route** by score (thresholds configurable):
  - **≥ `SCORE_AUTO_APPLY`** (default 90) → auto-apply with a generated cover
    letter — *only* when `AUTO_APPLY_ENABLED=true` (off by default).
  - **middle band** → sent to Telegram for manual decision (buttons:
    «Apply / Skip / Open on hh.ru»).
  - **< `SCORE_AUTO_SKIP`** (default 10) → auto-skip.
- **Cover letter** generation uses your `prompts/candidate.txt` profile,
  goes through an adversarial self-critique pass (regenerates on
  fabricated numbers / HR clichés / off-tone phrasing).
- **Multiple résumés** (optional): keep several targeted hh.ru résumés (one
  identity each — e.g. Product / Engineering / Project) and the bot routes
  each vacancy to the best-fitting one, writes the cover letter in that
  résumé's register, and selects it in the apply modal. Define the variants in
  `ai/resume_variants.py` and their search feeds in `resume-variants/feeds.yaml`
  (see the `.example`). Unconfigured, it runs with hh.ru's default single résumé.
- **Apply by link**: paste an hh.ru vacancy URL into the Telegram chat
  and the bot fetches it, scores it, drafts a cover letter, and shows
  it with Send / Rewrite / Cancel buttons — no need to wait for the
  autopilot to find it. Handles regional subdomains and tracking
  query params; refuses to double-apply.
- **Autopilot**: background search loop (paced to avoid hh.ru
  anti-bot throttling) + a periodic summary posted to your Telegram chat.
- **Inbox monitor** (`messages_loop`): checks hh.ru negotiations every
  5 min, tags incoming as `TEST_TASK` / `QUESTION` / `MESSAGE`, pings
  you in Telegram.
- **Company blacklist** (whole-word match), employer rating floor,
  «remote-only» filter, cross-city deduplication.
- **Telegram channel monitor** (optional): reads public job channels via
  their web mirror (`t.me/s/<channel>`, no API keys), extracts and scores
  posts, and pings relevant ones to your Telegram — runs alongside hh.ru.

## Disclaimer

> Using automation on hh.ru may violate the user agreement and result
> in account suspension. Use at your own risk. The bot is a pet project
> for a single user — multi-tenant deploy is not intended and the
> code has not undergone legal review.

## Stack

- **Python 3.10+** (requires `Browser | None` syntax)
- `aiogram 3.x` — Telegram bot
- `playwright` — hh.ru browser automation
- `httpx[socks]` — HTTP client with SOCKS5 proxy support
- `python-dotenv` — config
- `sqlite3` (Python stdlib) — storage

## Quick start

1. **Clone + install**
   ```bash
   git clone https://github.com/<YOUR_USERNAME>/hh-bot.git && cd hh-bot
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   ```
2. **Secrets** — `cp .env.example .env`, then fill `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ADMIN_ID`, `HH_LOGIN`, `DEEPSEEK_API_KEY` (+ optional `GROQ_API_KEY`
   fallback) and `CANDIDATE_NAME`.
3. **Your profile** — the two files that drive scoring + letters:
   ```bash
   cp prompts/analyzer_summary.example.txt prompts/analyzer_summary.txt  # short summary -> 0-100 score
   cp prompts/candidate.example.txt        prompts/candidate.txt         # full profile -> cover letters
   ```
   Fill both with your real data (they're gitignored, stay on your machine).
4. **How vacancies are found** (in `.env`): paste your résumé's "Похожие
   вакансии" URL into `HH_RESUME_SEARCH_URL` (recommended), and/or keep
   `HH_KEYWORD_FILTERS_ENABLED=true` and add filters via `/addfilter` in Telegram.
5. **Multiple résumés** (optional) — see the *Multiple résumés* section below.
6. **Run + log in**
   ```bash
   python main.py
   ```
   Then send `/login` in Telegram — a browser opens once; log in to hh.ru and
   the session is saved (headless from then on).

The sections below expand on each step.

## Installation

```bash
git clone https://github.com/<YOUR_USERNAME>/hh-bot.git
cd hh-bot

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
playwright install msedge   # for interactive login — visible Edge window
```

## Configuration

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

### Minimal `.env`:

```
TELEGRAM_BOT_TOKEN=...           # get from @BotFather
TELEGRAM_ADMIN_ID=...            # your Telegram user_id (ask @userinfobot)
HH_LOGIN=+79001234567            # phone number for hh.ru

DEEPSEEK_API_KEY=...             # primary LLM (platform.deepseek.com)
GROQ_API_KEY=...                 # free fallback (console.groq.com)

RESUME_FILE=resume.txt
```

### Your profile

The bot reads two gitignored files — copy the `.example` templates and fill in
your real data:

- `prompts/analyzer_summary.txt` — a SHORT summary (2-4 paragraphs); drives the
  0-100 relevance score the analyzer assigns each vacancy. Keep it brief — it's
  sent with every vacancy.
- `prompts/candidate.txt` — your FULL profile (products, verifiable metrics,
  an anti-fabrication list of things you have NOT done, opening samples); drives
  cover-letter generation.

`CANDIDATE_NAME` in `.env` is only the signature name.

### LLM provider

Out of the box the bot uses **DeepSeek** as the primary model and falls back
to **Groq** (free tier) if DeepSeek fails or its key is missing. Both use an
OpenAI-compatible Chat Completions API.

1. DeepSeek: sign up at https://platform.deepseek.com, create a key, set
   `DEEPSEEK_API_KEY`.
2. Groq (optional fallback): sign up at https://console.groq.com, create a
   key, set `GROQ_API_KEY`.

To use a different provider, point the client in `ai/llm_client.py` at it.

### Proxy (optional)

`HH_PROXY` proxies the hh.ru browser (needed if you run outside Russia —
hh.ru blocks foreign IPs). `TG_HTTP_PROXY` proxies the Telegram web mirror if
`t.me` is blocked on your network.

```
HH_PROXY=http://user:pass@host:port
```

## Run

```bash
python main.py
```

On first run the bot will prompt for authorization. In Telegram, send:

```
/login
```

A visible Edge browser window will open — log in to hh.ru manually
(captcha, SMS, whatever). The bot will pick up the session and switch
to headless mode.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Status, command list |
| `/login` | Open the browser for interactive hh.ru login |
| `/addfilter` | Add a search filter (title, keywords, city, salary, experience) |
| `/filters` | List active filters |
| `/search` | Run a manual search across all filters |
| `/autopilot` | Trigger one autopilot cycle manually |
| `/recheck` | Re-score un-scored vacancies |
| `/stats` | Application statistics |

## Architecture

```
hh-bot/
├── main.py                       # Entry point: bot + autopilot
├── config.py                     # Config from .env
├── ai/
│   ├── llm_client.py             # Universal OpenAI-compatible client
│   ├── analyzer.py               # Vacancy relevance scoring (generic,
│   │                             #   profile-driven; tunable in-file)
│   ├── vacancy_analyzer.py       # Deep structured JD analysis for
│   │                             #   cover-letter targeting
│   ├── cover_letter.py           # Cover letter generation + adversarial
│   │                             #   self-critique
│   ├── resume_variants.py        # Multi-résumé registry (identities +
│   │                             #   letter registers; edit to your own)
│   ├── variant_router.py         # Routes a vacancy to the best résumé
│   └── resume_feeds.py           # Loads per-variant feed config (yaml)
├── bot/
│   ├── handlers.py               # Telegram commands and callbacks
│   ├── autopilot.py              # Background search + filter + apply loop
│   ├── messages_loop.py          # hh.ru negotiations inbox monitor
│   ├── tg_loop.py                # Telegram channel monitor loop
│   └── keyboards.py              # Inline keyboards
├── parser/
│   ├── hh_client.py              # Playwright client for hh.ru
│   └── tg_client.py              # Reads TG channels via t.me/s/ web mirror
├── scripts/
│   ├── tg_pull_test.py           # Manual smoke test for TG channel parser
│   └── overlap_rescue_existing.py  # Retroactive profile-overlap rescue
├── prompts/
│   ├── candidate.example.txt     # Template — copy to candidate.txt
│   ├── analyzer_summary.example.txt
│   └── candidate.txt             # YOUR private profile (gitignored)
├── resume-variants/
│   └── feeds.example.yaml        # Template per-variant feed config
├── data/
│   └── tg_channels.example.yaml  # Template TG channel list
└── db/
    ├── models.py                 # SQLite schema
    └── storage.py                # CRUD
```

## Security

- Access to all commands is restricted by `TELEGRAM_ADMIN_ID` — the bot
  is single-user.
- hh.ru cookies are stored in `hh_cookies.json` (in `.gitignore`).
- Vacancy descriptions are isolated with markers in the LLM prompt —
  partial protection against prompt injection via vacancy text.
- All secrets via `.env`, nothing is hardcoded.

## Telegram channel monitor (optional)

The bot can also ingest job posts from public Telegram channels
(`@datasciencejobs`, `@workinai`, `@prog_jobs`, etc.) alongside hh.ru.
Channels are read through their **public web mirror**
(`https://t.me/s/<channel>`) — no API keys, no my.telegram.org, no login
or session files.

Setup:

1. Copy `data/tg_channels.example.yaml` to `data/tg_channels.yaml`, list
   the channels you want and tune `role_filter` per channel to your profile.
2. (Optional) Test it: `python -m parser.tg_client test` — prints the latest
   posts from each channel without running the bot.
3. Start the bot — the monitor runs automatically and every
   `TG_PULL_INTERVAL` (default 30 min) pushes relevant posts to your
   Telegram with «Open in Telegram / Draft letter / Skip» buttons.

The monitor only notifies (on Telegram you apply by messaging a recruiter
yourself), so it runs even in `MANUAL_MODE`. If `t.me` is blocked on your
network, set `TG_HTTP_PROXY` in `.env`.

## Multiple résumés (optional)

If you keep several targeted hh.ru résumés (one identity each — e.g. Product /
Engineering / Project), the bot routes each vacancy to the best-fitting one,
writes the cover letter in that résumé's register, and selects it in the apply
modal.

1. Edit `ai/resume_variants.py` — replace the 3 example variants with your own
   (short label, desired role, letter register, and the `signals` that identify
   the role). Add as many as you have résumés.
2. `cp resume-variants/feeds.example.yaml resume-variants/feeds.yaml`, then per
   variant paste its "Похожие вакансии" search URL (it carries the résumé hash)
   and the résumé title to select on apply. `feeds.yaml` is gitignored.

Leave both unconfigured to run with hh.ru's single default résumé.

## Known limitations

- **hh.ru selectors are hardcoded** — UI redesigns may require updates
  to `parser/hh_client.py`.
- **City grid** in `_resolve_area` covers only the 15 largest cities.
  Others are searched without region filter (with a warning in logs).
- **DB is sync** — `sqlite3` blocks the event loop at large volumes.
  For a single user this is fine.
- **No tests** on business logic.
- **TG channel monitor** only notifies — the autopilot does not apply for
  TG-sourced vacancies (on Telegram you apply by messaging a recruiter
  yourself); they are surfaced for manual review and letter drafting.

## License

[**PolyForm Noncommercial License 1.0.0**](https://polyformproject.org/licenses/noncommercial/1.0.0/) — full text in [`LICENSE`](LICENSE).

In short:

- **Allowed**: read the code, clone, fork, study, experiment, use for
  personal non-commercial purposes, in coursework and research, in
  hobby projects.
- **Allowed for organizations**: charitable, educational, research,
  government, healthcare, environmental.
- **Not allowed**: using the code in commercial products, reselling,
  building paid services or features on it, monetizing in any way.
- On distribution: preserve the license text and the author's copyright.

If you need commercial use — contact me for a separate commercial
license.
