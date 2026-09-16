#!/usr/bin/env python3
"""
Multi-source junior marketing/GTM job search.

Sources:
  - Greenhouse, Lever, Ashby: direct public JSON APIs, company slug guessed
    from company name.
  - Workday: direct undocumented CXS JSON API, per-company tenant config
    (WORKDAY_COMPANIES below) since tenant names can't be guessed reliably.
  - TalentBrew: direct HTML scrape of the server-rendered search-jobs page,
    per-company base URL config (TALENTBREW_COMPANIES below).
  - Adzuna, Remotive, Jobicy, Arbeitnow: broad aggregator APIs (catch
    companies not covered by any of the direct integrations above).
  - Google Custom Search: DORMANT. Google blocks the Custom Search JSON API
    for any Cloud project created after their new-customer cutoff (confirmed
    via a 403 PERMISSION_DENIED on this project) - not a config problem, it
    simply won't work for a newly created key. Left in place in case Google
    reopens access before the API's Jan 2027 shutdown; currently a no-op.

Run: python3 job_search.py
Output: appends new rows to OUTPUT_CSV, deduped against TRACKER_CSV and
        against OUTPUT_CSV itself, so it's safe to rerun anytime.
"""

import csv
import html
import os
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

TRACKER_CSV = "Job Applications Spring 2026 - Applied (3).csv"
OUTPUT_CSV = "job_search_results.csv"
OUTPUT_FIELDS = ["Title", "Company", "Location", "Date Found", "Source", "URL"]

COMPANIES = {
    "automotive": {
        "Ford": ["Ford Motor Company", "Ford Motor"],
        "GM": ["General Motors", "General Motors Company"],
        "GM Financial": ["General Motors Financial", "GM Financial Company"],
        "Stellantis": [],
        "Jeep": [],
        "Hyundai": ["Hyundai Motor America", "Hyundai Motor Company", "Hyundai Motor"],
        "Honda": ["American Honda Motor", "Honda Motor", "Honda Motor Co"],
        "Mazda": ["Mazda North American Operations", "Mazda Motor"],
        "Nissan": ["Nissan North America", "Nissan Motor"],
        "Kia": ["Kia America", "Kia Motors", "Kia Motors America"],
        "Mercedes-Benz USA": ["Mercedes-Benz", "Mercedes Benz", "Mercedes-Benz Group"],
        "Mitsubishi Motors": ["Mitsubishi Motors North America"],
        "Mitsubishi Heavy Industries": ["MHI"],
        "NXP Semiconductors": ["NXP"],
        "Lucid": ["Lucid Motors", "Lucid Group"],
        "Tesla": ["Tesla Motors"],
        "Scout Motors": [],
        "Zoox": [],
        "Applied Intuition": [],
        "JLR": ["Jaguar Land Rover"],
        "Bosch": ["Robert Bosch"],
    },
    "tech": {
        "OpenAI": [], "Anthropic": [], "Databricks": [], "Snowflake": [],
        "Perplexity": ["Perplexity AI"], "Cohere": [], "Hugging Face": [],
        "Together AI": ["Together"], "Glean": [], "Sierra": [],
        "Ramp": [], "Rippling": [], "Brex": [], "Notion": [],
        "Airtable": [], "Deel": [], "Plaid": [], "Instacart": [],
        "DoorDash": [], "Robinhood": [], "Chime": [], "Salesforce": [],
        "Revolut": [], "Google": ["Alphabet"], "Microsoft": [],
        "Amazon": [], "Meta": ["Facebook"], "Apple": [],
    },
    "cpg": {
        "Procter & Gamble": ["P&G"], "Unilever": [], "PepsiCo": [],
        "Coca-Cola": ["Coca Cola", "The Coca-Cola Company"],
        "Mondelez": ["Mondelez International"],
        "Nestle": ["Nestle USA"], "Kraft Heinz": [], "General Mills": [],
        "Colgate-Palmolive": ["Colgate"], "Clorox": [], "Kimberly-Clark": [],
        "L'Oreal": ["L'Oreal USA"], "Estee Lauder": ["The Estee Lauder Companies"],
        "Hershey": ["The Hershey Company"], "Mars": ["Mars Inc"], "Diageo": [],
        "Constellation Brands": [], "Keurig Dr Pepper": ["KDP"],
        "Danone": [], "Nike": [],
    },
}
ALL_COMPANIES = [name for group in COMPANIES.values() for name in group]
COMPANY_ALIASES = {name: aliases for group in COMPANIES.values() for name, aliases in group.items()}
COMPANY_TO_SECTOR = {name: sector for sector, group in COMPANIES.items() for name in group}
NON_CURATED_SECTOR = "other"  # local Bay Area companies outside the curated list

# Workday tenant/site can't be guessed from a company name, so these are
# found by hand (search "<company> myworkdayjobs.com") and confirmed
# working against the wday/cxs JSON API before being added here.
WORKDAY_COMPANIES = {
    "GM": {"tenant": "generalmotors", "wd": "wd5", "site": "Careers_GM"},
    "Nissan": {"tenant": "alliance", "wd": "wd3", "site": "nissanjobs"},
    "Stellantis": {"tenant": "stellantis", "wd": "wd3", "site": "External_Career_Site_ID01"},
}

# TalentBrew (Radancy) career sites: server-rendered search-jobs/<term> pages.
TALENTBREW_COMPANIES = {
    "Ford": "https://www.careers.ford.com",
}

# Broad net: title must contain one of these. Narrowed to marketing/GTM
# only — "strategy", "insights", "sales analyst", "revenue analyst", and
# "commercial analyst" used to qualify on their own, which is how pure
# corporate-strategy, sales-ops, and finance-analyst roles slipped in.
INCLUDE_KEYWORDS = [
    "marketing", "brand", "gtm", "go-to-market", "go to market", "growth",
]
# ...and none of these: seniority / SDR-AE false positives, engineering/
# technical/legal roles that happen to sit on a "Growth" or "GTM" team
# (e.g. "Software Engineer, GTM Innovation" is not a marketing job).
# Matched as whole words, so "vp" won't miss "VP," and won't wrongly hit
# words like "developer". Phrases like "private equity" are excluded as
# full phrases rather than the bare word "equity", since "equity" alone
# would wrongly exclude legitimate CPG titles like "Brand Equity".
EXCLUDE_KEYWORDS = [
    "senior", "sr", "lead", "founding", "director", "vp", "vice president",
    "head of", "principal", "staff", "chief", "svp", "evp", "sdr",
    "sales development representative", "account executive", "bdr",
    "business development representative", "engineer", "engineering",
    "developer", "scientist", "architect", "software", "devops", "sre",
    "designer", "recruiter", "recruiting", "counsel", "attorney",
    "technical", "leader", "partner",
    "private equity", "growth equity", "equity research", "venture capital",
    "investment banking",
    # Unlike "operations"/"insights"/"strategy" below, "finance"/"financial"
    # next to "marketing" in a title (e.g. "Strategic Finance, International
    # & Marketing") means an FP&A-style finance role that covers marketing
    # spend, not a marketing job — so these stay unconditionally excluded
    # rather than anchor-overridable.
    "finance", "financial",
]

# "Manager" alone almost always means 3+ years of experience at the
# companies on this list — a PMM or Growth Marketing Manager role at
# OpenAI/Anthropic-caliber companies is never entry-level. But CPG's
# classic entry-level track is literally titled "Assistant Brand Manager"
# or "Associate Marketing Manager", so "manager" only disqualifies a title
# when it isn't paired with one of those junior qualifiers.
MANAGER_JUNIOR_QUALIFIERS = ["assistant", "associate"]

# Words that signal a non-marketing org function (ops/strategy/finance/
# compliance) that often rides along on a "GTM" team name without the job
# itself being marketing — "GTM Strategy & Operations, Policy" and "GTM
# Compliance Analyst" are not marketing roles. But these same words show up
# in genuinely good junior marketing titles too ("Brand & Marketing
# Operations Associate", "Marketing Analyst, Consumer Insights"), so they
# only disqualify a title that has no unambiguous marketing anchor word.
AMBIGUOUS_FUNCTION_TERMS = [
    "strategy", "operations", "insights", "compliance", "policy", "legal",
    "risk", "audit", "accounting", "tax", "treasury", "underwriting",
    "actuarial", "systems",
]
MARKETING_ANCHOR_TERMS = ["marketing", "brand"]


def _matches_term(text, term):
    """Whole-word match for single words, substring match for phrases."""
    if " " in term:
        return term in text
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _is_unqualified_manager_title(t):
    return _matches_term(t, "manager") and not any(
        _matches_term(t, q) for q in MANAGER_JUNIOR_QUALIFIERS
    )


def _is_unanchored_ambiguous_title(t):
    has_ambiguous = any(_matches_term(t, k) for k in AMBIGUOUS_FUNCTION_TERMS)
    has_anchor = any(_matches_term(t, k) for k in MARKETING_ANCHOR_TERMS)
    return has_ambiguous and not has_anchor


BAY_AREA_KEYWORDS = [
    "san francisco", "bay area", "oakland", "san jose", "palo alto",
    "mountain view", "sunnyvale", "menlo park", "redwood city", "fremont",
    "berkeley", "south san francisco", "santa clara",
]
REMOTE_KEYWORDS = ["remote", "united states", "usa", "u.s.", "nationwide", "anywhere"]
# Some sources (Adzuna's /jobs/us/ endpoint) already guarantee US-only
# results but format location as "City, County" with no state name — an
# allowlist of state names would wrongly reject those. A blocklist of
# non-US countries is the right shape: permissive by default (ambiguous
# formats like "City, County" pass), only rejects a location that names an
# actual foreign country. Covers common English name + a few local-language
# variants (e.g. "brasil") seen in real results from Workday/TalentBrew.
NON_US_COUNTRIES = [
    "afghanistan", "albania", "algeria", "argentina", "armenia", "australia",
    "austria", "azerbaijan", "bahrain", "bangladesh", "belarus", "belgium",
    "bolivia", "bosnia", "brazil", "brasil", "bulgaria", "cambodia",
    "cameroon", "canada", "chile", "china", "colombia", "costa rica",
    "croatia", "cuba", "cyprus", "czech", "denmark", "ecuador", "egypt",
    "estonia", "ethiopia", "finland", "france", "germany",
    "ghana", "greece", "guatemala", "honduras", "hong kong", "hungary",
    "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel",
    "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kuwait",
    "latvia", "lebanon", "lithuania", "luxembourg", "malaysia", "mexico",
    "moldova", "monaco", "mongolia", "morocco", "myanmar", "nepal",
    "netherlands", "new zealand", "nicaragua", "nigeria", "norway", "oman",
    "pakistan", "panama", "paraguay", "peru", "philippines", "poland",
    "portugal", "qatar", "romania", "russia", "saudi arabia", "serbia",
    "singapore", "slovakia", "slovenia", "south africa", "south korea",
    "spain", "sri lanka", "sweden", "switzerland", "taiwan", "thailand",
    "tunisia", "turkey", "uganda", "ukraine", "united arab emirates",
    "united kingdom", " uk,", " uk ", "uruguay", "uzbekistan", "venezuela",
    "vietnam", "wales", "scotland", "england", "zambia", "zimbabwe",
]

ADZUNA_QUERY_TERMS = [
    "marketing analyst", "GTM analyst", "brand marketing",
    "marketing coordinator", "marketing associate", "growth marketing",
    "product marketing",
]

# Cap on how many new rows get written per run — applying to 194 roles a
# week isn't realistic, so only the highest-fit subset is surfaced. Rows
# that don't make the cut aren't recorded as "seen," so a later rerun
# re-evaluates the full pool and can resurface something that was just
# outside the top 50 before.
MAX_OUTPUT_ROWS = 50
MAX_PER_COMPANY = 5  # one hot company (e.g. a hiring spree at OpenAI) can't eat the whole list

# How many of the top rows go into the daily email digest (separate from the
# 50-row CSV cap — an email should be scannable in under a minute). The CSV's
# per-company cap of 5 is too loose for a 10-row digest — a single hiring
# spree could otherwise fill most of the email — so the digest gets its own,
# tighter cap.
DIGEST_TOP_N = 15
DIGEST_MAX_PER_COMPANY = 2
DIGEST_HTML_PATH = "email_digest.html"

SECTOR_COLORS = {
    "automotive": "#B45309",  # amber
    "tech": "#2563EB",        # blue
    "cpg": "#059669",         # green
    NON_CURATED_SECTOR: "#6B7280",  # gray
}

GOOGLE_MAX_QUERIES_PER_DAY = 100
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.3  # be polite between calls

HEADERS = {"User-Agent": "Mozilla/5.0 (job-search-script; personal use)"}


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slugify_variants(company):
    base = company.lower().strip()
    no_amp = base.replace("&", "and")
    variants = {
        no_amp.replace(" ", ""),
        no_amp.replace(" ", "-"),
        base.split()[0].lower(),
    }
    return [v for v in variants if v]


def passes_keyword_filter(title):
    t = title.lower()
    if not any(_matches_term(t, k) for k in INCLUDE_KEYWORDS):
        return False
    if any(_matches_term(t, k) for k in EXCLUDE_KEYWORDS):
        return False
    if _is_unqualified_manager_title(t):
        return False
    if _is_unanchored_ambiguous_title(t):
        return False
    return True


# Title-based filtering has a ceiling: plenty of postings with clean junior
# titles ("Analyst", "Specialist", non-manager) at competitive companies
# still explicitly ask for 4+ years in the actual description. Several
# sources (Greenhouse with content=true, Ashby, Lever, Adzuna, Remotive,
# Jobicy, Arbeitnow) already return the full description text for free —
# no extra API call — so read the real requirement when it's stated
# in-line rather than continuing to guess from the title alone. Workday and
# TalentBrew don't expose description text in their list endpoints (would
# need a second request per posting), so those two still rely on title
# filtering only.
YOE_MAX = 3
_YOE_PATTERN = re.compile(
    r"(\d{1,2})\s*(?:\+|-|to)?\s*\d{0,2}\+?\s*years?\s*"
    r"(?:of\s+)?(?:relevant\s+|professional\s+|related\s+)?experience",
    re.IGNORECASE,
)


def _strip_html(text):
    text = html.unescape(text or "")
    return re.sub(r"<[^>]+>", " ", text)


def extract_min_years_required(description):
    """First explicit 'N years [of] experience' figure in the text, or None
    if the description doesn't state one. Takes the first match rather than
    the smallest/largest, since postings that list both a minimum and a
    preferred/bonus qualification almost always state the actual minimum
    first."""
    text = _strip_html(description)
    match = _YOE_PATTERN.search(text)
    return int(match.group(1)) if match else None


def passes_yoe_filter(description):
    """Permissive when no description is available or no explicit YOE is
    stated — this only screens OUT postings that name a number, it never
    screens IN based on absence of one."""
    if not description:
        return True
    min_years = extract_min_years_required(description)
    return min_years is None or min_years <= YOE_MAX


# Corporate-suffix words stripped before comparing two company names, so
# "Ford Motor Company" and "Toyota Motor Corporation" reduce to their brand
# name — but "Toyota Of Scranton" (a franchise dealer, not corporate Toyota)
# correctly stays distinct, since "of scranton" isn't a suffix.
_COMPANY_SUFFIX_WORDS = {
    "inc", "llc", "corp", "corporation", "co", "company", "usa", "motor",
    "motors", "international", "group", "plc", "ltd", "na", "holdings",
    "of", "america", "gmbh", "ag", "the",
}


def normalize_company_for_match(name):
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = n.split()
    while tokens and tokens[-1] in _COMPANY_SUFFIX_WORDS:
        tokens.pop()
    while tokens and tokens[0] in _COMPANY_SUFFIX_WORDS:
        tokens.pop(0)
    return " ".join(tokens)


def matches_curated_company(candidate_name):
    """Returns the canonical curated company name if candidate_name matches
    (by name or known alias, suffix-normalized), else None."""
    cand_norm = normalize_company_for_match(candidate_name)
    if not cand_norm:
        return None
    for canonical, aliases in COMPANY_ALIASES.items():
        for variant in (canonical, *aliases):
            if normalize_company_for_match(variant) == cand_norm:
                return canonical
    return None


def is_bay_area(location):
    loc = (location or "").lower()
    return any(k in loc for k in BAY_AREA_KEYWORDS)


def passes_location_filter(location):
    # Permissive by default (many US sources format location as "City,
    # County" with no state/country at all) — only reject a location that
    # explicitly names a non-US country, unless it's also flagged remote.
    loc = (location or "").lower()
    if not loc:
        return True  # unknown location, don't drop it
    is_remote = any(k in loc for k in REMOTE_KEYWORDS)
    is_foreign = any(c in loc for c in NON_US_COUNTRIES)
    return is_remote or not is_foreign


# Full US state names, for the Workday-specific check below. Not used as
# the general-purpose location filter's allowlist — that broke Adzuna's
# "City, County" format (no state present) — but Workday tenants shared
# across a multinational (e.g. Nissan's "alliance" tenant serves the whole
# Renault-Nissan-Mitsubishi group) can return European postings with only
# a bare city name and no country at all, which the country blocklist above
# can't catch. Workday results get this extra, stricter check.
US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
]


def looks_us_location(location):
    loc = (location or "").lower()
    if not loc:
        return True  # unknown, don't drop
    if is_bay_area(loc) or any(k in loc for k in REMOTE_KEYWORDS):
        return True
    if "united states" in loc or "usa" in loc or "u.s." in loc:
        return True
    return any(state in loc for state in US_STATE_NAMES)


# Fit score = 60% location + 40% match (company fit + role/title fit).
# Weights per the user's own breakdown, not a made-up default.
_STRONG_TITLE_TERMS = ["analyst", "coordinator", "associate", "specialist"]
_CORE_SUBJECT_TERMS = ["marketing", "brand", "gtm", "go-to-market", "go to market", "growth"]


def compute_fit_score(row):
    location_score = 1.0 if is_bay_area(row["Location"]) else 0.4

    company_score = 1.0 if matches_curated_company(row["Company"]) else 0.5

    t = row["Title"].lower()
    role_score = 0.4
    if any(_matches_term(t, k) for k in _STRONG_TITLE_TERMS):
        role_score += 0.35
    if any(_matches_term(t, k) for k in _CORE_SUBJECT_TERMS):
        role_score += 0.25
    role_score = min(role_score, 1.0)

    match_score = (company_score + role_score) / 2
    return 0.6 * location_score + 0.4 * match_score


def load_dedup_keys():
    keys = set()
    if os.path.exists(TRACKER_CSV):
        with open(TRACKER_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            reader.fieldnames = [fn.strip() for fn in reader.fieldnames]
            for row in reader:
                title = row.get("Title", "")
                company = row.get("Company", "")
                if title and company:
                    keys.add((normalize(title), normalize(company)))
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get("Title", "")
                company = row.get("Company", "")
                if title and company:
                    keys.add((normalize(title), normalize(company)))
    return keys


def make_row(title, company, location, source, url):
    return {
        "Title": title.strip(),
        "Company": company.strip(),
        "Location": (location or "").strip(),
        "Date Found": date.today().isoformat(),
        "Source": source,
        "URL": url,
    }


# --------------------------------------------------------------------------
# DIRECT ATS APIS
# --------------------------------------------------------------------------

def fetch_greenhouse(company):
    for slug in slugify_variants(company):
        # content=true costs nothing extra (still one request) but includes
        # the full job description, which is what lets passes_yoe_filter
        # catch a stated "5+ years" that the title alone never would.
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json().get("jobs", [])
        if not jobs:
            continue
        return [
            (j.get("title", ""), j.get("location", {}).get("name", ""),
             j.get("absolute_url", ""), j.get("content", ""))
            for j in jobs
        ]
    return []


def fetch_lever(company):
    for slug in slugify_variants(company):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json()
        if not jobs:
            continue
        return [
            (j.get("text", ""), (j.get("categories") or {}).get("location", ""),
             j.get("hostedUrl", ""), j.get("descriptionPlain", j.get("description", "")))
            for j in jobs
        ]
    return []


def fetch_ashby(company):
    for slug in slugify_variants(company):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json().get("jobs", [])
        if not jobs:
            continue
        return [
            (j.get("title", ""), j.get("location", ""), j.get("jobUrl", ""),
             j.get("descriptionHtml", j.get("descriptionPlain", "")))
            for j in jobs
        ]
    return []


def fetch_direct_ats():
    rows = []
    for company in ALL_COMPANIES:
        for fetch_fn, source in (
            (fetch_greenhouse, "Greenhouse"),
            (fetch_lever, "Lever"),
            (fetch_ashby, "Ashby"),
        ):
            try:
                jobs = fetch_fn(company)
            except Exception as e:
                print(f"  [warn] {source} lookup failed for {company}: {e}")
                continue
            for title, location, url, description in jobs:
                if (passes_keyword_filter(title) and passes_location_filter(location)
                        and passes_yoe_filter(description)):
                    rows.append(make_row(title, company, location, source, url))
            time.sleep(REQUEST_DELAY)
    return rows


def fetch_workday_company(company, cfg):
    base = f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/{cfg['site']}"
    api = (f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/"
           f"wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs")
    rows = []
    for term in ADZUNA_QUERY_TERMS:
        try:
            resp = requests.post(
                api, headers={**HEADERS, "Content-Type": "application/json"},
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Workday lookup failed for {company} ('{term}'): {e}")
            continue
        for j in resp.json().get("jobPostings", []):
            title = j.get("title", "")
            location = j.get("locationsText", "")
            url = base + j.get("externalPath", "")
            if (passes_keyword_filter(title) and passes_location_filter(location)
                    and looks_us_location(location)):
                rows.append(make_row(title, company, location, "Workday", url))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_talentbrew_company(company, base_url):
    rows = []
    for term in ("marketing", "gtm", "strategy", "sales-analyst"):
        url = f"{base_url}/search-jobs/{term}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] TalentBrew lookup failed for {company} ('{term}'): {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for h2 in soup.select("h2.search-results-list__job-title"):
            a = h2.find("a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "")
            job_url = base_url + href if href.startswith("/") else href
            loc_el = h2.parent.select_one(".job-location")
            location = loc_el.get_text(strip=True) if loc_el else ""
            if passes_keyword_filter(title) and passes_location_filter(location):
                rows.append(make_row(title, company, location, "TalentBrew", job_url))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_configured_ats():
    """Companies with a hand-confirmed Workday tenant or TalentBrew site."""
    rows = []
    for company, cfg in WORKDAY_COMPANIES.items():
        try:
            rows.extend(fetch_workday_company(company, cfg))
        except Exception as e:
            print(f"  [warn] Workday lookup crashed for {company}: {e}")
    for company, base_url in TALENTBREW_COMPANIES.items():
        try:
            rows.extend(fetch_talentbrew_company(company, base_url))
        except Exception as e:
            print(f"  [warn] TalentBrew lookup crashed for {company}: {e}")
    return rows


# --------------------------------------------------------------------------
# AGGREGATOR APIS (not restricted to COMPANIES by construction, so results
# get an extra gate below: a company outside the curated list is only worth
# surfacing if it's local — no point knowing an unknown company in Raleigh
# has a marketing coordinator opening if relocating for it was never on the
# table. A curated-list company (Ford, Nike, etc.) is worth seeing anywhere.)
# --------------------------------------------------------------------------

def gate_and_canonicalize(company, location):
    """(keep, company_name_to_use) — canonicalizes to the curated name on a
    match (so "Ford Motor Company" shows as "Ford"); otherwise keeps the
    raw name, only if the role is Bay Area."""
    canonical = matches_curated_company(company)
    if canonical:
        return True, canonical
    if is_bay_area(location):
        return True, company
    return False, company


def adzuna_location(location_obj, fallback):
    # display_name is "City, County" (e.g. "Corvallis, Benton County") —
    # not meaningful to someone outside that county. area is the structured
    # breakdown ["US", "State", "County", "City"]; use it to build
    # "City, State" instead.
    area = (location_obj or {}).get("area") or []
    if len(area) >= 2 and area[0] == "US":
        state = area[1]
        city = area[-1] if len(area) > 2 and area[-1] != state else None
        return f"{city}, {state}" if city else state
    return fallback


def fetch_adzuna(app_id, app_key):
    rows = []
    if not app_id or not app_key:
        print("  [skip] Adzuna: no API key in .env")
        return rows
    for term in ADZUNA_QUERY_TERMS:
        url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": term,
            "results_per_page": 50,
            "content-type": "application/json",
        }
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Adzuna query '{term}' failed: {e}")
            continue
        for j in resp.json().get("results", []):
            title = j.get("title", "")
            company = (j.get("company") or {}).get("display_name", "")
            loc_obj = j.get("location") or {}
            location = adzuna_location(loc_obj, loc_obj.get("display_name", ""))
            url_ = j.get("redirect_url", "")
            description = j.get("description", "")
            if not (passes_keyword_filter(title) and passes_location_filter(location)
                    and passes_yoe_filter(description)):
                continue
            keep, company_name = gate_and_canonicalize(company, location)
            if keep:
                rows.append(make_row(title, company_name, location, "Adzuna", url_))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_remotive():
    rows = []
    url = "https://remotive.com/api/remote-jobs"
    try:
        resp = requests.get(url, params={"category": "marketing"},
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Remotive failed: {e}")
        return rows
    for j in resp.json().get("jobs", []):
        title = j.get("title", "")
        company = j.get("company_name", "")
        location = j.get("candidate_required_location", "")
        url_ = j.get("url", "")
        description = j.get("description", "")
        if not (passes_keyword_filter(title) and passes_location_filter(location)
                and passes_yoe_filter(description)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Remotive", url_))
    return rows


def fetch_jobicy():
    rows = []
    url = "https://jobicy.com/api/v2/remote-jobs"
    try:
        resp = requests.get(url, params={"count": 50, "tag": "marketing"},
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Jobicy failed: {e}")
        return rows
    for j in resp.json().get("jobs", []):
        title = j.get("jobTitle", "")
        company = j.get("companyName", "")
        location = j.get("jobGeo", "")
        url_ = j.get("url", "")
        description = j.get("jobDescription", j.get("jobExcerpt", ""))
        # Jobicy tags each posting with an explicit level, so use it
        # directly rather than relying on the description text alone.
        if (j.get("jobLevel") or "").strip().lower() in ("senior", "director"):
            continue
        if not (passes_keyword_filter(title) and passes_location_filter(location)
                and passes_yoe_filter(description)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Jobicy", url_))
    return rows


def fetch_arbeitnow():
    rows = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Arbeitnow failed: {e}")
        return rows
    for j in resp.json().get("data", []):
        title = j.get("title", "")
        company = j.get("company_name", "")
        location = j.get("location", "")
        url_ = j.get("url", "")
        description = j.get("description", "")
        if not (passes_keyword_filter(title) and passes_location_filter(location)
                and passes_yoe_filter(description)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Arbeitnow", url_))
    return rows


# --------------------------------------------------------------------------
# GOOGLE CUSTOM SEARCH (rate-capped, targeted per company)
# --------------------------------------------------------------------------

# Confirmed dead for this key: Google returns 403 PERMISSION_DENIED for any
# Cloud project created after their new-customer cutoff, regardless of API
# key/cx config. Left disabled rather than deleted in case Google reopens
# access before the API's Jan 2027 shutdown - flip to True to retry.
GOOGLE_CSE_ENABLED = False


def fetch_google_cse(api_key, cse_id):
    rows = []
    if not GOOGLE_CSE_ENABLED:
        print("  [skip] Google CSE: disabled (confirmed 403 for new Cloud projects)")
        return rows
    if not api_key or not cse_id:
        print("  [skip] Google CSE: GOOGLE_CSE_ID not set in .env yet")
        return rows
    query_count = 0
    for company in ALL_COMPANIES:
        if query_count >= GOOGLE_MAX_QUERIES_PER_DAY:
            print(f"  [cap] Hit {GOOGLE_MAX_QUERIES_PER_DAY} Google queries, stopping")
            break
        q = (
            f'"{company}" (marketing OR GTM OR "go-to-market" OR strategy OR "sales analyst") '
            f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com '
            f'OR site:myworkdayjobs.com)'
        )
        params = {"key": api_key, "cx": cse_id, "q": q, "num": 10}
        try:
            resp = requests.get("https://www.googleapis.com/customsearch/v1",
                                 params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Google CSE query failed for {company}: {e}")
            query_count += 1
            continue
        query_count += 1
        for item in resp.json().get("items", []):
            title = item.get("title", "")
            url_ = item.get("link", "")
            if passes_keyword_filter(title):
                rows.append(make_row(title, company, "", "Google CSE", url_))
        time.sleep(REQUEST_DELAY)
    print(f"  Google CSE queries used today: {query_count}/{GOOGLE_MAX_QUERIES_PER_DAY}")
    return rows


# --------------------------------------------------------------------------
# SELECTION: per-company cap + sector-balanced top-N
# --------------------------------------------------------------------------

def apply_company_cap(rows_sorted_by_score, cap):
    counts = {}
    kept = []
    for row in rows_sorted_by_score:
        c = row["Company"]
        if counts.get(c, 0) >= cap:
            continue
        counts[c] = counts.get(c, 0) + 1
        kept.append(row)
    return kept


def select_top_rows(all_rows, total_slots, company_cap):
    """Rank within each sector by fit score, cap per company, then split
    total_slots across sectors as evenly as possible — but a sector with
    fewer qualifying rows than its even share doesn't hog unused slots;
    those get redistributed to sectors that still have more to offer."""
    by_sector = {}
    for row in all_rows:
        sector = COMPANY_TO_SECTOR.get(row["Company"], NON_CURATED_SECTOR)
        by_sector.setdefault(sector, []).append(row)

    for sector, rows in by_sector.items():
        rows.sort(key=compute_fit_score, reverse=True)
        by_sector[sector] = apply_company_cap(rows, company_cap)

    pools = {s: rows for s, rows in by_sector.items() if rows}
    allocation = {s: 0 for s in pools}
    slots_left = total_slots
    while slots_left > 0 and pools:
        share = max(1, slots_left // len(pools))
        for sector in list(pools.keys()):
            available = len(pools[sector]) - allocation[sector]
            take = min(share, available, slots_left)
            if take <= 0:
                continue
            allocation[sector] += take
            slots_left -= take
            if allocation[sector] >= len(pools[sector]):
                del pools[sector]
            if slots_left <= 0:
                break

    selected = []
    for sector, count in allocation.items():
        selected.extend(by_sector[sector][:count])
    selected.sort(key=compute_fit_score, reverse=True)
    return selected


# --------------------------------------------------------------------------
# EMAIL DIGEST (HTML)
# --------------------------------------------------------------------------

def _esc(s):
    return html.escape(s or "", quote=True)


# Fixed sector display order (rather than alphabetical or fit-score order)
# so the digest reads the same shape every day — automotive/tech/cpg first
# since those are the named target industries, "other" (aggregator finds
# outside the curated company list) last.
_SECTOR_DISPLAY_ORDER = ["automotive", "tech", "cpg", NON_CURATED_SECTOR]


def render_html_digest(rows):
    """Plain inline-styled HTML, table-based layout for email-client safety.
    Gmail's send pipeline strips `background`/`background-color` outright
    (confirmed by inspecting the stored message), so every accent here is
    done with `border` and `color` instead — no fills."""
    today = date.today().strftime("%B %-d, %Y") if os.name != "nt" else date.today().strftime("%B %d, %Y")

    grouped = {}
    for row in rows:
        sector = COMPANY_TO_SECTOR.get(row["Company"], NON_CURATED_SECTOR)
        grouped.setdefault(sector, []).append(row)
    ordered_sectors = [s for s in _SECTOR_DISPLAY_ORDER if s in grouped]
    ordered_sectors += [s for s in grouped if s not in ordered_sectors]

    sections = []
    counter = 0
    for sector in ordered_sectors:
        color = SECTOR_COLORS.get(sector, SECTOR_COLORS[NON_CURATED_SECTOR])
        sections.append(f"""
        <tr>
          <td style="padding:24px 24px 10px 24px;">
            <div style="display:inline-block;font-size:12px;font-weight:800;letter-spacing:.08em;
                        text-transform:uppercase;color:{color};border-bottom:2.5px solid {color};
                        padding-bottom:5px;">
              {_esc(sector)}
            </div>
          </td>
        </tr>""")
        for row in grouped[sector]:
            counter += 1
            location = _esc(row["Location"]) or "Location not listed"
            sections.append(f"""
        <tr>
          <td style="padding:0 24px 16px 24px;">
            <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td width="30" valign="top">
                  <div style="width:22px;height:22px;border:1.5px solid {color};border-radius:11px;
                              color:{color};font-size:11px;font-weight:800;text-align:center;
                              line-height:21px;">{counter}</div>
                </td>
                <td style="border-left:3px solid {color};padding-left:14px;">
                  <div style="font-size:16px;font-weight:700;color:#111827;line-height:1.35;">
                    {_esc(row["Title"])}
                  </div>
                  <div style="font-size:13.5px;color:{color};font-weight:700;margin-top:3px;">
                    {_esc(row["Company"])}
                  </div>
                  <div style="font-size:12.5px;color:#6B7280;margin-top:2px;">
                    {location} &middot; via {_esc(row["Source"])}
                  </div>
                  <div style="margin-top:10px;">
                    <a href="{_esc(row["URL"])}"
                       style="display:inline-block;border:1.5px solid {color};color:{color};
                              font-size:13px;font-weight:700;text-decoration:none;padding:7px 14px;
                              border-radius:6px;">
                      View &amp; Apply &rarr;
                    </a>
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>""")

    sections_html = "".join(sections) if sections else """
        <tr><td style="padding:32px 24px;text-align:center;color:#6B7280;font-size:14px;">
          No new qualifying roles found today — nothing worth interrupting your day for.
        </td></tr>"""

    return f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="background:#FFFFFF;border-radius:10px;overflow:hidden;max-width:600px;width:100%;
                      border-top:4px solid #111827;">
          <tr>
            <td style="padding:24px 24px 18px 24px;border-bottom:1px solid #E5E7EB;">
              <div style="font-size:20px;font-weight:800;color:#111827;">Job Search Digest</div>
              <div style="font-size:13px;color:#6B7280;margin-top:4px;">{_esc(today)} &middot; {len(rows)} new role{"s" if len(rows) != 1 else ""}</div>
            </td>
          </tr>
          {sections_html}
          <tr>
            <td style="padding:18px 24px;text-align:center;border-top:1px solid #E5E7EB;">
              <div style="font-size:12px;color:#9CA3AF;">
                Automated daily search &middot; full list always in job_search_results.csv
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    load_dotenv()
    adzuna_id = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "")
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    google_cse_id = os.environ.get("GOOGLE_CSE_ID", "")

    seen = load_dedup_keys()
    print(f"Loaded {len(seen)} existing title+company pairs to dedup against.")

    all_new_rows = []
    counts = {}

    print("Checking Greenhouse / Lever / Ashby for target companies...")
    for row in fetch_direct_ats():
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Checking Workday / TalentBrew for configured companies...")
    for row in fetch_configured_ats():
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Adzuna...")
    for row in fetch_adzuna(adzuna_id, adzuna_key):
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Remotive, Jobicy, Arbeitnow...")
    for fetch_fn in (fetch_remotive, fetch_jobicy, fetch_arbeitnow):
        for row in fetch_fn():
            key = (normalize(row["Title"]), normalize(row["Company"]))
            if key in seen:
                continue
            seen.add(key)
            all_new_rows.append(row)
            counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Google Custom Search (targeted, rate-capped)...")
    for row in fetch_google_cse(google_key, google_cse_id):
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    top_rows = select_top_rows(all_new_rows, MAX_OUTPUT_ROWS, MAX_PER_COMPANY)

    file_exists = os.path.exists(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in top_rows:
            writer.writerow(row)

    written_counts = {}
    for row in top_rows:
        written_counts[row["Source"]] = written_counts.get(row["Source"], 0) + 1

    digest_rows = apply_company_cap(top_rows, DIGEST_MAX_PER_COMPANY)[:DIGEST_TOP_N]
    with open(DIGEST_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_html_digest(digest_rows))
    print(f"Wrote {len(digest_rows)}-row email digest to {DIGEST_HTML_PATH}.")

    print(f"\nFound {len(all_new_rows)} qualifying new rows across all sources; "
          f"wrote the top {len(top_rows)} by fit score to {OUTPUT_CSV}.")
    print("Found by source:")
    for source, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {source}: {n}")
    print("Written by source:")
    for source, n in sorted(written_counts.items(), key=lambda x: -x[1]):
        print(f"  {source}: {n}")
    if len(all_new_rows) > MAX_OUTPUT_ROWS:
        print(f"({len(all_new_rows) - MAX_OUTPUT_ROWS} more found but not written — "
              f"rerun later and they'll be re-evaluated fresh.)")


if __name__ == "__main__":
    main()
