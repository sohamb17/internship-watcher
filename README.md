# internship-watcher

Polls several job sources every 30 minutes and pushes new software / data / ML
internship listings to [ntfy](https://ntfy.sh). Pure standard library, no
dependencies, runs free on GitHub Actions.

## Sources

| Source | What it is | Auth |
|---|---|---|
| `simplify` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships), read from `.github/scripts/listings.json` | none |
| `ats` | Public job-board APIs of individual companies — Greenhouse, Lever, Ashby | none |

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
| `US_ONLY` | `1` | `0` keeps non-US ATS postings |
| `MIN_YEAR` | `2027` | Drop ATS postings naming an earlier season year |

## Configuration

`boards.json` maps ATS slug → display name:

```json
{
  "greenhouse": { "stripe": "Stripe" },
  "lever":      { "palantir": "Palantir" },
  "ashby":      { "notion": "Notion" }
}
```

Find a slug in a company's careers URL — `boards.greenhouse.io/SLUG`,
`jobs.lever.co/SLUG`, `jobs.ashbyhq.com/SLUG`. Verify before adding:

```bash
curl -s https://boards-api.greenhouse.io/v1/boards/SLUG/jobs | head -c 200
curl -s "https://api.lever.co/v0/postings/SLUG?mode=json"    | head -c 200
curl -s https://api.ashbyhq.com/posting-api/job-board/SLUG   | head -c 200
```

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