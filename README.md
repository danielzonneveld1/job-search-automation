# Job Search Automation

A Python script that searches Greenhouse, Lever, Ashby, Workday, and TalentBrew directly,
plus Adzuna/Remotive/Jobicy/Arbeitnow as a broader net, for junior marketing/GTM roles
across a curated list of automotive, tech, and CPG companies.

## What it does

- **Direct ATS integration**: queries Greenhouse, Lever, and Ashby's public job board
  APIs, plus hand-confirmed Workday tenants and TalentBrew career sites, for a curated
  company list.
- **Aggregator fallback**: broadens the net via Adzuna, Remotive, Jobicy, and Arbeitnow,
  gated so a non-curated company only surfaces if the role is local (no relocation ask)
  or the company matches the curated list via alias-aware fuzzy matching (so "Ford Motor
  Company" and "Toyota Motor Corporation" resolve correctly, while "Toyota Of Scranton" —
  a franchise dealer, not corporate — correctly does not).
- **Keyword + seniority filtering**: whole-word matched include/exclude lists tuned for
  junior (0-3 year) titles, filtering out roles that pass on title alone but turn out to
  require 5-10+ years on the actual posting.
- **Location filtering**: permissive-by-default with a non-US country blocklist, since
  many sources report "City, County" with no state/country at all.
- **Fit scoring**: weighted 60% location / 40% company+role match, with a per-company cap
  and sector-balanced (automotive/tech/CPG) selection so one hot company or sector can't
  crowd out the rest of a capped output list.
- **Dedup**: cross-references a tracker CSV of applications already made, so reruns only
  ever surface genuinely new roles.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file with:

```
ADZUNA_APP_ID=your_app_id
ADZUNA_APP_KEY=your_app_key
GOOGLE_API_KEY=
GOOGLE_CSE_ID=
```

(Adzuna keys are free at [developer.adzuna.com](https://developer.adzuna.com). Google
Custom Search is currently disabled in the script — see the note in `job_search.py` —
so those two can be left blank.)

## Run

```bash
python3 job_search.py
```

Appends new rows to `job_search_results.csv`, deduped against your own applications
tracker CSV. Safe to rerun anytime — only genuinely new roles get written.

## Config

Company list, keyword filters, location rules, and scoring weights are all plain config
blocks at the top of `job_search.py` — no code changes needed to add a company or adjust
what counts as a match.
