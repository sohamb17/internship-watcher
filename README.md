# internship-watcher

Polls several job sources every 30 minutes and pushes new software / data / ML
internship listings to [ntfy](https://ntfy.sh). Pure standard library, no
dependencies, runs free on GitHub Actions.

## Sources

| Source | What it is | Auth |
|---|---|---|
| `simplify` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships), read from `.github/scripts/listings.json` | none |
| `ats` | Official company career portals — Greenhouse, Lever, Ashby, SmartRecruiters, Workday, amazon.jobs | none |
| `vansh` | [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) — Simplify's `listings.json` schema without `category`, so titles are filtered | none |
| `speedy` | [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) — USA internship section only | none |
| `zshah` | [zshah101/Automated-List-…-Tech-Internships](https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships) `data/jobs.json` — has `is_open`, `category` and `sponsorship`; citizens-only and no-sponsorship roles dropped | none |
| `zapply` | [zapplyjobs/Internships-2027](https://github.com/zapplyjobs/Internships-2027) | none |
| `sndsh` | [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) — hand-curated; 🔒 / 🛂 / 🇺🇸 rows dropped | none |
| `jobright` | [jobright-ai/2026-Software-Engineer-Internship](https://github.com/jobright-ai/2026-Software-Engineer-Internship) — named 2026 but a live rolling list carrying 2027 roles | none |
| `nointernship` | [nointernship.com/internships](https://www.nointernship.com/internships) — the 25 newest listings from the page's schema.org `ItemList`, joined to each card's location and apply link | none |

The five table sources were taken from the upstream list in
[DereC4/internships-and-newgrad](https://github.com/DereC4/internships-and-newgrad)'s
`go/main.go`. DereC4 itself was a source briefly and is now retired: it
re-aggregates these, so it only added lag and duplicates. Its new-grad
upstreams are deliberately not included.

**nointernship.com** is read politely: one request per run to the public
`/internships` page, which its robots.txt allows. Its `/api/` (which serves
everything past the first 25) is disallowed there and is not used. The page
lists newest first and the watcher polls every 30 minutes, so the newest 25 is
the slice that matters. Its locations are bare city names ("Radnor",
"Topeka"), so for this source only, a listing is kept unless its location is
clearly foreign.

Each run sends **one** push covering every source, newest first.

Not included: Instagram (e.g. @zero2sudo stories). Stories need a logged-in
session, expire in 24 hours, are images rather than text, and scraping them is
against Instagram's terms and actively blocked. Those accounts largely repost
listings from the repos above anyway.

### Why `listings.json` and not the README

The repo publishes the same data twice: as HTML `<table>` blocks in the README,
and as structured JSON at `.github/scripts/listings.json`. The JSON carries
fields the README flattens into emoji or drops entirely:

| Field | Replaces |
|---|---|
| `active`, `is_visible` | the 🔒 closed flag |
| `sponsorship` | the 🛂 and 🇺🇸 flags |
| `degrees` | the 🎓 flag — and distinguishes "PhD/MBA only" from "advanced degree welcome" |
| `date_posted` | the age column the HTML parser discarded |
| `terms` | nothing; the README has no season field |
| `id` (stable UUID) | the `Company\|Role` key, which was not unique |

That last one matters: the old key collapsed the same role posted in several
cities into one entry, so a second city never notified. The UUID key also means
retitling a posting no longer looks like a new job.

### Why not LinkedIn

LinkedIn has no public jobs API. The only programmatic routes are the
`jobs-guest` HTML endpoint (rate-limited and IP-blocked quickly) and paid
scrapers, both against LinkedIn's terms.

The `ats` source targets the layer underneath instead. Greenhouse, Lever and
Ashby are where most postings originate; LinkedIn is a syndication surface on
top. Reading the boards directly is keyless, allowed, and typically *earlier*.
It also picks up employers the aggregated lists cover thinly — the quant shops
(Jump, IMC, Point72, Squarepoint) are the clearest example. The trade-off is
coverage: only companies in `boards.json` are seen.

## Filtering

**`simplify`** keeps a listing when it is `active` and `is_visible`, its
`category` is Software or AI/ML/Data, it offers sponsorship, its `terms` include
a wanted term (or are absent / `N/A`), and its `degrees` include at least one
at or below Master's.

That last rule drops postings wanting *only* PhD/MBA/JD/MD while keeping
Bachelor's-only ones — a Master's student is eligible for those, since a
bachelor's is a floor, not a ceiling.

University and college employers are dropped (`EXCLUDE_ACADEMIC=0` to keep
them); their "Undergraduate Research Assistant" postings are categorised as
Software upstream but are campus jobs.

**No keyword filter is applied to `simplify` titles.** Simplify's own
`category` is the better signal. A title filter measurably loses good
postings — Jane Street, Google Student Researcher, ByteDance, Anthropic
Fellows, Blackstone Summer Analyst and Zoox all post real internships whose
titles never contain the word "intern".

**`ats`** has no categories, so it does filter on title: an intern/co-op word
plus a technical word, minus a blocklist for roles that match by accident
("Account Development Representative Intern"). An unambiguous engineering term
overrides that blocklist, so `Software Engineer Intern - … and Brand` survives.

Because company portals are global, the ATS source **requires positive US
evidence** from a location before keeping a posting (a blank location is still
kept). Note that the state-abbreviation match is deliberately case-sensitive:
under a case-insensitive match the codes `IN`, `OR`, `OK`, `ME`, `HI`, `LA`,
`DE`, `CO`, `ID`, `PA` and `MA` all match ordinary English words, and
"BangPa-in, Thailand" reads as Indiana.

### Recruiting term

Only `SEASONS` (default **Summer 2027 and Fall 2027**) get through. "Winter
2027" and "Spring 2027" are January-start co-op terms — a different cycle —
and they arrive unlabelled more often than not: vanshb03 lists 65 of Tesla's
70 open roles as `season: Winter` while every title omits it. The term is read
from every place a source states it: Simplify's `terms`, vanshb03's and
zshah101's `season`, and titles in every source ("Winter 2027 Co-op",
"Co-op - January 2027", "(Jan - Jun 2027)", "Fall/Winter 2026"). A listing that
names no term is kept, since most Summer postings never say.

Two phrasings are deliberately not read as a term: graduation dates
("Grad Year: Spring 2027" describes the applicant, and is a Summer role), and
the end of a dated range ("Sept 2026 - May 2027" starts in Fall 2026).

### Environment

| Var | Default | Meaning |
|---|---|---|
| `NTFY_TOPIC` | — | ntfy topic. Unset ⇒ notifications print to stdout |
| `STATE_FILE` | `state.json` | Seen-listing state |
| `BOARDS_FILE` | `boards.json` | ATS board config |
| `SOURCES` | all | Comma list to restrict, e.g. `SOURCES=ats` |
| `SEASONS` | `Summer 2027,Fall 2027` | Terms to accept; listings that name no term are kept |
| `MAX_AGE_DAYS` | unset | Drop listings posted more than N days ago |
| `EXCLUDE_ACADEMIC` | `1` | `0` keeps university employers |
| `WD_PAGES` | `10` | Workday pages (×20 jobs) polled per company per run |
| `WD_QUERY` | `intern` | Workday server-side search term |
| `SR_MAX` | `600` | Max SmartRecruiters postings paged per company |
| `AMZN_MAX` | `400` | Max amazon.jobs results paged |
| `US_ONLY` | `1` | `0` keeps non-US ATS postings |
| `MIN_YEAR` | `2027` | Drop ATS postings naming an earlier season year |
| `DIGEST_FILE` | `digest.md` | Where the full list is written each run |
| `DIGEST_URL` | auto | Link the notification opens; derived from `GITHUB_REPOSITORY` |
| `NTFY_BUDGET` | `3500` | Max notification body bytes before linking out (ntfy's hard limit is 4096) |
| `SEEN_TTL_DAYS` | `60` | Days after last sighting before a listing is forgotten |
| `WD_PER_HOST` | `5` | Max concurrent requests to one Workday host |

## Watched company portals (484 boards)

The original ~100 boards were hand-picked. The rest were **discovered from the
other sources**: every aggregator row that links straight to a Greenhouse,
Lever, Ashby, SmartRecruiters or Workday posting reveals that company's board.
Polling the board directly reports a posting when it goes up, before any list
picks it up.

Selection: every discovered Greenhouse / Lever / Ashby / SmartRecruiters board
with at least one relevant internship (one request each), but only Workday
boards with **5+** (each costs up to `WD_PAGES` = 10 requests, and Workday's
"intern" search is not reliably relevance-ordered — Micron's intern titles run
`[2, 2, 0, 0, 1, 1, 0, 1…]` across pages — so pages cannot be cut short). All
were verified live, then de-duplicated case-insensitively (Workday and
Ashby URLs ignore case, so `NVIDIAExternalCareerSite` and
`nvidiaexternalcareersite` are one board) and stripped of test and
executive-search boards. `source_ats` also skips case-variant duplicates, so
the same board is never polled twice. Re-run the discovery when the sources
grow.

**A newly added board seeds silently.** Its existing listings are recorded,
not reported, exactly as a new source's are; only postings that appear on it
afterwards alert. Every board that answers is recorded, including ones with no
relevant listing yet, so a quiet board's first internship *is* reported.

**Workday politeness.** Tenants share a few hosts (`wd1` alone carries 45 of
them). At most `WD_PER_HOST` (5) requests run against one host at a time, and
HTTP 429 is retried with backoff. Unthrottled, the expansion drew 429s from
`wd1`; at 5 per host it draws none and the whole ATS pass takes ~75 s.

### Title filter

Polling whole company boards exposes every engineering discipline a company
hires, so the title filter (used by every source without a `category`) now
also rejects civil, structural, mechanical, manufacturing, electrical,
process, quality and similar engineering roles, titles made only of generic
words ("Intern Engineer - Summer 2027"), and full-time roles that mention
interns only as eligibility ("New Grad Engineer I (For Current & Recent
Interns Only)"). A clear software signal still overrides: "Embedded Software
Intern - Mechanical Systems" and "Electrical/Software Engineering Co-op" pass.
Mobile, gameplay, "Member of Technical Staff" and "Technology Intern" titles
are recognised as software.

## Configuration

`boards.json` maps each company to a display name. Four platforms take a slug
from the careers URL; **Workday takes the portal URL itself**:

```json
{
  "greenhouse":      { "stripe": "Stripe" },
  "lever":           { "palantir": "Palantir" },
  "ashby":           { "notion": "Notion" },
  "smartrecruiters": { "WesternDigital": "Western Digital" },
  "amazon":          { "intern": "Amazon" },
  "workday": {
    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite": "NVIDIA"
  }
}
```

Slugs come from the careers URL — `boards.greenhouse.io/SLUG`,
`jobs.lever.co/SLUG`, `jobs.ashbyhq.com/SLUG`,
`jobs.smartrecruiters.com/SLUG`. For Workday, paste the portal URL as you'd
visit it; tenant, host and site are parsed out (`tenant|host|site` also works).

Verify before adding:

```bash
curl -s https://boards-api.greenhouse.io/v1/boards/SLUG/jobs        | head -c 200
curl -s "https://api.lever.co/v0/postings/SLUG?mode=json"           | head -c 200
curl -s https://api.ashbyhq.com/posting-api/job-board/SLUG          | head -c 200
curl -s https://api.smartrecruiters.com/v1/companies/SLUG/postings  | head -c 200
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"appliedFacets":{},"limit":20,"offset":0,"searchText":"intern"}' \
  https://TENANT.wdN.myworkdayjobs.com/wday/cxs/TENANT/SITE/jobs    | head -c 200
```

### MAG-7 coverage

All seven are already covered by the `simplify` source, so direct polling buys
*latency*, not coverage. Only two are reachable without scraping:

| Company | Direct | Why |
|---|---|---|
| NVIDIA | yes — Workday | portal API |
| Amazon | yes — `amazon.jobs` | own search JSON. Its `is_intern` field returns the string `'None'`, so it is ignored and the title filter does the work |
| Apple | no | `jobs.apple.com` API returns 401 without a session token |
| Alphabet | no | no public careers API; the site is a JS app |
| Meta | no | `metacareers.com` is GraphQL behind per-session tokens |
| Tesla | no | `tesla.com/cua-api` returns 403 to non-browser clients (bot protection) |
| Microsoft | no | `apply.careers.microsoft.com` runs Eightfold AI, whose `/api/apply/v2/jobs` returns 403 to non-browser clients. The correct domain returns 403 while a wrong one returns 404, so the endpoint is real and deliberately closed. Qualcomm and IBM behave identically — it is platform-wide, not Microsoft-specific |

Getting Apple, Alphabet, Meta, Tesla or Microsoft would mean defeating bot
protection or driving a headless browser — the same objection that rules out
LinkedIn — and a GitHub runner is a datacenter IP, exactly what those blocks
target. The `simplify` source already carries all of them, typically within a
day.

Untried route if the lead time matters enough: Microsoft's older public search
endpoint at `gcsservices.careers.microsoft.com/search/api/v1/search`, which is
not Eightfold. It could not be reached from the machine this was built on, so
it is neither supported nor ruled out.

### Workday caveats

Its search is fuzzy — searching "intern" at Salesforce returns
`Account Partner - Financial Services` — so titles are filtered locally. It
returns no real timestamp either, only `"Posted 11 Days Ago"`, so ages from
Workday are day-resolution and `30+ Days Ago` floors at 30. And `locationsText`
is often `"2 Locations"`, in which case the location is recovered from the job
URL slug. Portals are large and paged at 20, so each is polled `WD_PAGES` deep
per run (default 10 ≈ 200 jobs/company).

A board that 404s is logged and skipped, not fatal. Every shipped slug was
verified live.

## Behaviour that matters

**A source that fails keeps its previous state.** It never re-notifies on
recovery, and one source failing does not stop the others. The run exits
non-zero so Actions goes red, but state is committed first.

**A source with no state seeds silently.** Adding or renaming a source never
floods you on its first run.

**Cross-source duplicates are dropped.** Matched on a normalised company+role
key with season, year, punctuation and word order stripped; `simplify` wins.
Dedupe keys are persisted, so an outage cannot unmask duplicates a source was
previously suppressing.

**One push per run, and a digest behind it.** Each run writes `digest.md`
and the push links to it: every new posting with company, role, location,
posting age, source and apply link, in one table sorted **newest posting
first** (unknown dates last, same-age roles grouped by company), then an index
of every watched company's careers page. The push lists postings in the same
order, filling up to ntfy's 4 KB limit and linking to the digest for the rest.
Older postings that a source adds or reopens late are still reported, with
their age shown — they sort to the bottom rather than being dropped.

**Reworded duplicates are caught too.** Sources word the same job
differently and even name the company differently: Shield AI's "Summer 2027
Intern - Embedded Firmware - 5810" came back as "Embedded Firmware Intern" from
"Shield", and Costco's "CDS Intern - Software Engineer - IT Retail Media" as
"Software Engineer Intern, IT Retail Media". Within the same company — one name
being the start of the other, 6+ letters, so "Costco" matches "Costco
Wholesale" but "Intel" does not match "Intelligent Systems" — titles are
reduced to their meaningful words (degree tags, job numbers and the company's
own name dropped; engineering → engineer) and count as one role when the word
sets match, or when one contains the other with only short codes like "CDS"
left over. Short words that carry meaning (AI, ML, iOS, SRE…) still separate
roles, and titles under three words must match exactly, so "Software Engineer
Intern" and "…, Backend" stay distinct. Replayed over the first 143 alerts of
the v4 history, this would have removed 8, all genuine duplicates, and kept
every lookalike-but-distinct pair (RTX's two teams, Booz Allen's two cities,
AmEx's undergraduate vs Masters programs).

**Partial ATS failure is tolerated; majority failure is not.** If more than
half the boards are unreachable the source refuses to run rather than
concluding every posting vanished.

**Notifications show posting age**, e.g. `[15d ago]`, and a degree ceiling when
the role does not accept a bachelor's, e.g. `[3d ago · Master's/PhD]`. Age is
never used to suppress unless `MAX_AGE_DAYS` is set — a listing can be old and
still newly visible to the watcher, which is a normal and occasionally
surprising outcome.

## Reliable 30-minute runs

GitHub's `schedule` trigger is best-effort. On this repo it fired every 3–7
hours instead of every 30 minutes, and no cron expression fixes that. The
workflow is therefore driven by an external scheduler calling the
`workflow_dispatch` API, with GitHub's cron kept as a two-hourly fallback.

1. **Token.** GitHub → Settings → Developer settings → Fine-grained tokens →
   *Generate*. Repository access: only `internship-watcher`. Permissions:
   **Actions: Read and write** (nothing else). Pick an expiry and set a
   reminder to rotate it.
2. **Scheduler.** At [cron-job.org](https://cron-job.org) (free), create a
   job:
   - URL: `https://api.github.com/repos/sohamb17/internship-watcher/actions/workflows/watch.yml/dispatches`
   - Schedule: every 30 minutes
   - Advanced → Request method: `POST`
   - Headers: `Authorization: Bearer <token>`,
     `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main"}`

   A `204` response means a run was queued.

The workflow's `concurrency` group keeps overlapping triggers from running at
once. The repo is public, so Actions minutes are free and unlimited.

## State format

```json
{
  "version": 4,
  "seen":  { "simplify": { "<uuid>": 20394 }, "ats": { "greenhouse:stripe:8031833": 20394 } },
  "roles": { "stripe|engineer software": 20394 }
}
```

Values are the UTC day number on which the entry was last observed. A
listing alerts only if **both** its ID in that source and its normalised
company+role are absent. Every observed listing refreshes both, whether it is
reported or suppressed as a duplicate, and entries expire `SEEN_TTL_DAYS`
(default 60) after they were last seen.

This replaced v3, which stored only the listings that survived the current
run. Anything that dropped out for a single run — a Workday page reshuffle, a
role suppressed as a duplicate of another source, or one of several
same-titled roles (Goldman's London / Dallas / Salt Lake City) losing a
first-come dedupe race when upstream reordered — was forgotten and re-alerted
on return. Across 341 runs that produced 1,600 repeat alerts, one listing 73
times. Same-titled roles now merge into one alert ("+2 more").

Day resolution is deliberate. State is committed every run, and timestamps
that change every run would rewrite the whole file each time; with days, a
run's diff is just its new listings.

Migration from v3 is automatic. Because v3 is missing whatever was flapping,
the first v4 run alerts only on postings up to a day old, so it cannot replay
the old repeats.

## Local run

```bash
python3 watch.py                          # prints instead of pushing
SOURCES=ats python3 watch.py              # one source
STATE_FILE=/tmp/x.json python3 watch.py   # throwaway baseline
```