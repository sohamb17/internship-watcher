# internship-watcher

Polls several job sources every 30 minutes and pushes new software / data / ML
internship listings to [ntfy](https://ntfy.sh). Pure standard library, no
dependencies, runs free on GitHub Actions.

## Sources

| Source | What it is | Auth |
|---|---|---|
| `github` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) README | none |
| `ats` | Public job-board APIs of individual companies — Greenhouse, Lever, Ashby | none |

### Why not LinkedIn

LinkedIn has no public jobs API. The only programmatic routes are the
`jobs-guest` HTML endpoint (rate-limited and IP-blocked quickly — a GitHub
Actions runner gets throttled within days) and paid third-party scrapers, and
both sit on the wrong side of LinkedIn's terms of service.

The `ats` source targets the layer underneath instead. Greenhouse, Lever and
Ashby are where most of these postings originate; LinkedIn is a syndication
surface on top. Reading the ATS boards directly is keyless, documented,
allowed, and typically **earlier** than the LinkedIn listing. It also picks up
employers the aggregated lists cover thinly — the quant shops (Jump, IMC,
Point72, Squarepoint) are the clearest example.

The trade-off is coverage: this only sees companies listed in `boards.json`,
whereas LinkedIn sees everyone. Adding a company is one line.

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
`jobs.lever.co/SLUG`, `jobs.ashbyhq.com/SLUG`. Verify one before adding it:

```bash
curl -s https://boards-api.greenhouse.io/v1/boards/SLUG/jobs | head -c 200
curl -s "https://api.lever.co/v0/postings/SLUG?mode=json"    | head -c 200
curl -s https://api.ashbyhq.com/posting-api/job-board/SLUG   | head -c 200
```

A board that 404s is logged and skipped, not fatal. Every slug shipped in
`boards.json` was verified live.

### Environment

| Var | Default | Meaning |
|---|---|---|
| `NTFY_TOPIC` | — | ntfy topic. Unset ⇒ notifications print to stdout |
| `STATE_FILE` | `state.json` | Seen-listing state |
| `BOARDS_FILE` | `boards.json` | ATS board config |
| `SOURCES` | all | Comma list to restrict, e.g. `SOURCES=ats` |
| `US_ONLY` | `1` | `0` keeps non-US ATS postings |
| `MIN_YEAR` | `2027` | Drop ATS postings naming an earlier season year |

Filters for the `github` source stay in `watch.py`: `SECTIONS` picks the
README sections, `EXCLUDE_FLAGS` drops 🛂 no-sponsorship, 🇺🇸 citizenship-only
and 🔒 closed rows.

## Behaviour that matters

**A source that fails keeps its previous state.** It never re-notifies on
recovery, and a failure in one source does not stop the others. The run still
exits non-zero so the Actions run goes red, but state is committed first.

**A source with no state seeds silently.** Adding a source never floods you on
its first run. Upgrading from the old flat-list `state.json` migrates in place
and preserves every existing key, so the first run after upgrading is quiet.

**Cross-source duplicates are dropped.** The two sources word titles
differently (`Software Engineer, Intern` vs `Software Engineer Intern (Summer
2027)`), so matching is on a normalised company + role key with season, year,
punctuation and word order stripped. `github` wins. Dedupe keys are persisted,
so a GitHub outage cannot unmask duplicates it was previously suppressing.

**Partial ATS failure is tolerated; majority failure is not.** If more than
half the boards are unreachable the source refuses to run rather than
concluding every posting disappeared.

## State format

```json
{
  "version": 2,
  "sources": { "github": ["Company|Role"], "ats": ["greenhouse:stripe:8031833"] },
  "dedupe":  { "github": ["company|normalised role"] }
}
```

`github` keys stay `Company|Role` for backward compatibility. `ats` keys use
the ATS job id, so retitling a posting does not re-notify.

## Known quirk

The `github` state key is `Company|Role`, which is not unique — the same role
posted in several cities collapses to one key (503 rows → 423 keys today). A
second city for a role you already have will not notify. Fixing it means
adding location to the key, which invalidates existing state; the first run
after that change would report every listing as new unless the state file is
deleted and re-seeded in the same commit.

## Local run

```bash
python3 watch.py                       # prints instead of pushing
SOURCES=ats python3 watch.py           # one source
STATE_FILE=/tmp/x.json python3 watch.py  # seed a throwaway baseline
```