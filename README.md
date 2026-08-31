# internship-watcher

Polls several job sources every 30 minutes and pushes new software / data / ML
internship listings to [ntfy](https://ntfy.sh). Pure standard library, no
dependencies, runs free on GitHub Actions.

## Sources

| Source | What it is | Auth |
|---|---|---|
| `simplify` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships), read from `.github/scripts/listings.json` | none |
| `ats` | Official company career portals — Greenhouse, Lever, Ashby, SmartRecruiters, Workday, amazon.jobs | none |

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
`category` is Software or AI/ML/Data, it offers sponsorship, its `terms` name a
year in `TERM_YEARS` (or are absent / `N/A`), and its `degrees` include at
least one at or below Master's.

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

### Environment

| Var | Default | Meaning |
|---|---|---|
| `NTFY_TOPIC` | — | ntfy topic. Unset ⇒ notifications print to stdout |
| `STATE_FILE` | `state.json` | Seen-listing state |
| `BOARDS_FILE` | `boards.json` | ATS board config |
| `SOURCES` | all | Comma list to restrict, e.g. `SOURCES=ats` |
| `TERM_YEARS` | `2027` | Season years to accept |
| `MAX_AGE_DAYS` | unset | Drop listings posted more than N days ago |
| `EXCLUDE_ACADEMIC` | `1` | `0` keeps university employers |
| `WD_PAGES` | `10` | Workday pages (×20 jobs) polled per company per run |
| `WD_QUERY` | `intern` | Workday server-side search term |
| `SR_MAX` | `600` | Max SmartRecruiters postings paged per company |
| `AMZN_MAX` | `400` | Max amazon.jobs results paged |
| `US_ONLY` | `1` | `0` keeps non-US ATS postings |
| `MIN_YEAR` | `2027` | Drop ATS postings naming an earlier season year |

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

**Partial ATS failure is tolerated; majority failure is not.** If more than
half the boards are unreachable the source refuses to run rather than
concluding every posting vanished.

**Notifications show posting age**, e.g. `[15d ago]`, and a degree ceiling when
the role does not accept a bachelor's, e.g. `[3d ago · Master's/PhD]`. Age is
never used to suppress unless `MAX_AGE_DAYS` is set — a listing can be old and
still newly visible to the watcher, which is a normal and occasionally
surprising outcome.

## State format

```json
{
  "version": 3,
  "sources": { "simplify": ["<uuid>"], "ats": ["greenhouse:stripe:8031833"] },
  "dedupe":  { "simplify": ["company|normalised role"] }
}
```

Migration is automatic. A v1 flat list or a v2 `github` source is read, then
retired — `simplify` is a new source name, so it seeds silently and the first
run after upgrading sends nothing.

## Local run

```bash
python3 watch.py                          # prints instead of pushing
SOURCES=ats python3 watch.py              # one source
STATE_FILE=/tmp/x.json python3 watch.py   # throwaway baseline
```