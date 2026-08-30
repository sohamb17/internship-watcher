#!/usr/bin/env python3
"""
Watch several sources for new internship listings and push them to ntfy.

Sources
  github  SimplifyJobs/Summer2027-Internships README. The README uses HTML
          <table> blocks, not markdown pipe tables, so a line diff does not
          work. Rows are parsed properly and keyed on (company, role) so that
          edits to existing rows are ignored.
  ats     Public job-board APIs of individual companies: Greenhouse, Lever and
          Ashby. Keyless, documented, ToS-clean, and the origin of most
          postings that later show up on LinkedIn. Keyed on the ATS job id, so
          a retitled posting is not reported twice.

Design notes
  * State is namespaced per source. A source that errors keeps its previous
    state untouched, so a transient failure can never cause a notification
    storm on the next run. A v1 flat list is migrated on read.
  * A source that has no state yet seeds silently. Adding a source therefore
    never floods you on its first run.
  * ATS rows that duplicate something already on the GitHub list are dropped,
    matched on a normalised company+role key (title wording differs between
    the two sources, so exact matching would not catch them).

Env
  NTFY_TOPIC   required for push; prints to stdout when unset
  STATE_FILE   default state.json
  BOARDS_FILE  default boards.json
  SOURCES      comma-separated list to restrict active sources (default: all)
  US_ONLY      "0" to keep non-US postings from the ATS source (default: on)
  MIN_YEAR     drop ATS postings naming an earlier season year (default 2027)
"""
import concurrent.futures
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

STATE = os.environ.get("STATE_FILE", "state.json")
BOARDS_FILE = os.environ.get("BOARDS_FILE", "boards.json")
TOPIC = os.environ.get("NTFY_TOPIC")
ONLY = {s.strip() for s in os.environ.get("SOURCES", "").split(",") if s.strip()}
US_ONLY = os.environ.get("US_ONLY", "1") != "0"
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2027"))

UA = "internship-watcher (+github actions; contact via repo issues)"

GH_README = ("https://raw.githubusercontent.com/SimplifyJobs/"
             "Summer2027-Internships/dev/README.md")
GH_LINK = "https://github.com/SimplifyJobs/Summer2027-Internships"

# Sections you care about. Drop entries to widen or narrow.
SECTIONS = {"Software Engineering", "Data Science, AI & Machine Learning"}

# 🛂 no sponsorship · 🇺🇸 citizenship required · 🔒 closed
EXCLUDE_FLAGS = "🛂🇺🇸🔒"

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️⃣]")


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout))


def clean(s):
    s = re.sub(r"<br\s*/?>", " / ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


# Cross-source duplicate detection. The two sources word titles differently
# ("Software Engineer, Intern" vs "Software Engineer Intern (Summer 2027)"),
# so the identity key has to survive season suffixes, punctuation and word
# order. Only used for dedupe, never for state.
_SEASON = re.compile(
    r"\((?:summer|winter|fall|autumn|spring)[^)]*\)|"
    r"\b(?:summer|winter|fall|autumn|spring)\s*20\d\d\b", re.I)
_NOISE = re.compile(
    r"\b(intern|interns|internship|internships|co|op|coop|program|programme|"
    r"student|students|university|new|grad|graduate|undergrad|undergraduate|"
    r"summer|winter|fall|autumn|spring|20\d\d|the|and|of|for|a|an)\b", re.I)
_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|"
    r"labs|lab|group|holdings|systems)\b", re.I)


def canon(company, role):
    c = _SUFFIX.sub(" ", EMOJI.sub("", company).lower())
    c = re.sub(r"[^a-z0-9]", "", c)
    r = _NOISE.sub(" ", _SEASON.sub(" ", EMOJI.sub("", role).lower()))
    r = re.sub(r"[^a-z0-9]+", " ", r)
    r = " ".join(sorted(set(r.split())))
    return f"{c}|{r}"


# --------------------------------------------------------------------------
# location + relevance filters (ATS only; the GitHub list is pre-filtered)
# --------------------------------------------------------------------------

_US_STRONG = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    r"VA|WA|WV|WI|WY|DC)\b|"
    r"\b(united states|u\.?s\.?a\.?|alabama|alaska|arizona|arkansas|"
    r"california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    r"massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|"
    r"nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|"
    r"washington|west virginia|wisconsin|wyoming)\b", re.I)

_NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ottawa|calgary|waterloo|ontario|"
    r"quebec|british columbia|united kingdom|england|scotland|wales|london|"
    r"manchester|edinburgh|glasgow|bristol|leeds|ireland|dublin|cork|"
    r"germany|berlin|munich|m[uü]nchen|hamburg|frankfurt|cologne|france|"
    r"paris|lyon|toulouse|spain|madrid|barcelona|valencia|portugal|lisbon|"
    r"porto|netherlands|amsterdam|rotterdam|utrecht|belgium|brussels|"
    r"switzerland|zurich|z[uü]rich|geneva|lausanne|austria|vienna|italy|"
    r"milan|rome|turin|poland|warsaw|krakow|krak[oó]w|wroclaw|gdansk|"
    r"czech|prague|brno|hungary|budapest|romania|bucharest|cluj|bulgaria|"
    r"sofia|greece|athens|sweden|stockholm|gothenburg|norway|oslo|denmark|"
    r"copenhagen|finland|helsinki|estonia|tallinn|latvia|riga|lithuania|"
    r"vilnius|ukraine|kyiv|kiev|serbia|belgrade|croatia|zagreb|turkey|"
    r"istanbul|ankara|israel|tel aviv|jerusalem|haifa|uae|dubai|abu dhabi|"
    r"saudi|riyadh|qatar|doha|egypt|cairo|nigeria|lagos|abuja|kenya|nairobi|"
    r"ghana|accra|south africa|johannesburg|cape town|india|bengaluru|"
    r"bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|"
    r"kolkata|ahmedabad|pakistan|lahore|karachi|islamabad|bangladesh|dhaka|"
    r"sri lanka|colombo|nepal|kathmandu|china|beijing|shanghai|shenzhen|"
    r"guangzhou|hangzhou|hong kong|macau|taiwan|taipei|japan|tokyo|osaka|"
    r"kyoto|korea|seoul|busan|singapore|malaysia|kuala lumpur|penang|"
    r"indonesia|jakarta|thailand|bangkok|vietnam|hanoi|ho chi minh|"
    r"philippines|manila|cebu|australia|sydney|melbourne|brisbane|perth|"
    r"canberra|new zealand|auckland|wellington|brazil|s[aã]o paulo|"
    r"rio de janeiro|argentina|buenos aires|chile|santiago|colombia|bogot[aá]|"
    r"medellin|peru|lima|uruguay|montevideo|mexico|guadalajara|monterrey|"
    r"costa rica|san jos[eé], costa rica|panama|iceland|reykjavik|"
    r"luxembourg|malta|cyprus|morocco|casablanca|tunisia|armenia|yerevan|"
    r"georgia, country|kazakhstan|almaty|emea|apac|latam|emea/apac)\b", re.I)

_US_COUNTRY = {"usa", "us", "u.s.", "u.s.a.", "united states",
               "united states of america"}

_INTERN = re.compile(r"\b(intern|interns|internship|internships|co-?op|"
                     r"co-?ops)\b", re.I)

# Mirrors the GitHub source's two sections: SWE, and Data/AI/ML.
_TECH = re.compile(
    r"\b(software|swe|engineer|engineering|developer|development|programmer|"
    r"data|analytics|analyst|machine learning|ml|ai|artificial intelligence|"
    r"deep learning|nlp|computer vision|research|scientist|science|quant|"
    r"quantitative|trading|trader|computer|cs|security|infosec|cryptography|"
    r"infrastructure|platform|backend|back-end|frontend|front-end|"
    r"full.?stack|systems|distributed|cloud|devops|sre|reliability|network|"
    r"database|compiler|kernel|embedded|firmware|hardware|silicon|asic|fpga|"
    r"robotics|autonomy|perception|graphics|simulation|technical)\b", re.I)

# Titles that satisfy _TECH by accident: "Account Development Representative",
# "Technical Recruiter", "Product Design Intern" and friends.
_NOT_TECH = re.compile(
    r"\b(account (development|executive|manager|management)|"
    r"business development|sales|seller|revenue ops|"
    r"recruit(er|ing|ment)?|talent|sourcer|campus ambassador|"
    r"marketing|brand|communications|public relations|social media|content|"
    r"copywrit|people ops|human resources|hr business|benefits|compensation|"
    r"legal|counsel|paralegal|compliance|audit|tax|payroll|accounting|"
    r"customer (success|support|experience)|community|"
    r"(product|ux|ui|graphic|visual|brand|motion|industrial) design|"
    r"designer|partnerships|procurement|facilities|workplace|"
    r"executive assistant|office manager|real estate|supply chain|"
    r"logistics|warehouse|driver|technician)\b", re.I)

_YEAR = re.compile(r"\b(20\d\d)\b")


def is_us(loc, country=None):
    """Positive US evidence wins; otherwise reject only on a clear foreign
    signal. Unknown/blank locations are kept — under-notifying is worse."""
    if country and country.strip().lower() in _US_COUNTRY:
        return True
    t = (loc or "").strip()
    if not t:
        return True
    if _US_STRONG.search(t):
        return True
    if _NON_US.search(t):
        return False
    if country:
        return False
    return True


def tech(title):
    return bool(_TECH.search(title)) and not _NOT_TECH.search(title)


def relevant(title, loc, country=None):
    if not _INTERN.search(title) or not tech(title):
        return False
    years = [int(y) for y in _YEAR.findall(title)]
    if years and max(years) < MIN_YEAR:
        return False
    if US_ONLY and not is_us(loc, country):
        return False
    return True


# --------------------------------------------------------------------------
# source: github readme
# --------------------------------------------------------------------------

def parse_readme(txt):
    rows, last_company = [], None
    parts = re.split(r"\n\s*##\s+(.+?Internship Roles)\s*\n", txt)
    for i in range(1, len(parts), 2):
        section = EMOJI.sub("", parts[i]).replace("Internship Roles", "").strip()
        for tr in re.findall(r"<tr>(.*?)</tr>", parts[i + 1], re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 4:
                continue
            company = EMOJI.sub("", clean(tds[0])).strip()
            if company.startswith("↳") or not company:
                company = last_company or "?"
            else:
                last_company = company
            m = re.search(r'href="([^"]+)"', tds[3])
            rows.append({
                "section": section,
                "company": company,
                "role": clean(tds[1]),
                "loc": clean(tds[2])[:60],
                "url": html.unescape(m.group(1)) if m else "",
                "flags": "".join(f for f in "🛂🇺🇸🔒🔥🎓" if f in tr),
            })
    return rows


def source_github(_boards):
    txt = fetch(GH_README, timeout=60).decode("utf-8")
    rows = parse_readme(txt)
    if len(rows) < 100:
        raise RuntimeError(
            f"only parsed {len(rows)} rows - README format probably changed")
    out = []
    for r in rows:
        if r["section"] not in SECTIONS:
            continue
        if any(f in r["flags"] for f in EXCLUDE_FLAGS):
            continue
        # v1-compatible state key: do not change, it matches existing state.json
        r["sk"] = f"{r['company']}|{r['role']}"
        r["dk"] = canon(r["company"], r["role"])
        r["link"] = GH_LINK
        out.append(r)
    return out


# --------------------------------------------------------------------------
# source: public ATS job boards
# --------------------------------------------------------------------------

def _label(slug, given=None):
    """Display name. boards.json may map a slug to a proper name; otherwise
    fall back to a title-cased slug, which is right often enough."""
    if given:
        return given
    s = re.sub(r"[-_]+", " ", slug).strip()
    return s.title() if s.islower() else s


def board_greenhouse(slug):
    d = fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=25)
    out = []
    for j in d.get("jobs", []):
        title = (j.get("title") or "").strip()
        loc = ((j.get("location") or {}).get("name") or "").strip()
        out.append({"id": str(j.get("id")), "title": title, "loc": loc,
                    "country": None, "url": j.get("absolute_url") or "",
                    "etype": None})
    return out


def board_lever(slug):
    d = fetch_json(
        f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=25)
    out = []
    for j in d:
        cats = j.get("categories") or {}
        out.append({"id": str(j.get("id")),
                    "title": (j.get("text") or "").strip(),
                    "loc": (cats.get("location") or "").strip(),
                    "country": None,
                    "url": j.get("hostedUrl") or "",
                    "etype": cats.get("commitment")})
    return out


def board_ashby(slug):
    d = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=25)
    out = []
    for j in d.get("jobs", []):
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append({"id": str(j.get("id")),
                    "title": (j.get("title") or "").strip(),
                    "loc": (j.get("location") or "").strip(),
                    "country": addr.get("addressCountry"),
                    "url": j.get("jobUrl") or "",
                    "etype": j.get("employmentType")})
    return out


ATS = {"greenhouse": board_greenhouse,
       "lever": board_lever,
       "ashby": board_ashby}


def source_ats(boards):
    # Each ATS section is either {"slug": "Display Name", ...} or ["slug", ...]
    targets = []
    for kind, entry in boards.items():
        if kind not in ATS:
            continue
        if isinstance(entry, dict):
            targets += [(kind, slug, name) for slug, name in entry.items()]
        else:
            targets += [(kind, slug, None) for slug in entry]
    if not targets:
        raise RuntimeError("no ATS boards configured")

    def one(t):
        kind, slug, name = t
        try:
            return kind, slug, name, ATS[kind](slug), None
        except Exception as e:                       # noqa: BLE001
            return kind, slug, name, [], f"{type(e).__name__}: {e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, targets))

    failed = [f"{k}/{s} ({e})" for k, s, _, _, e in results if e]
    if failed:
        print(f"[ats] {len(failed)} board(s) unreachable: "
              f"{', '.join(failed[:6])}", file=sys.stderr)
    # A network blip must not look like "every posting disappeared" and then
    # re-notify everything next run. Bail out and keep the previous state.
    if len(failed) > len(targets) // 2:
        raise RuntimeError(
            f"{len(failed)}/{len(targets)} boards failed - refusing to run")

    out = []
    for kind, slug, name, jobs, err in results:
        if err:
            continue
        company = _label(slug, name)
        for j in jobs:
            # Ashby/Lever tag the posting type explicitly, which catches
            # internships whose title never says "intern".
            intern_type = (j.get("etype") or "").lower().startswith("intern")
            title = j["title"]
            if not (relevant(title, j["loc"], j.get("country"))
                    or (intern_type and tech(title)
                        and (not US_ONLY
                             or is_us(j["loc"], j.get("country"))))):
                continue
            out.append({
                "section": "ATS",
                "company": company,
                "role": title,
                "loc": (j["loc"] or "")[:60],
                "url": j["url"],
                "flags": "",
                "sk": f"{kind}:{slug}:{j['id']}",
                "dk": canon(company, title),
                "link": j["url"],
            })
    print(f"[ats] {len(targets) - len(failed)} boards ok, {len(out)} relevant")
    return out


SOURCES = {"github": source_github, "ats": source_ats}
PRIORITY = ["github", "ats"]          # earlier sources win a duplicate


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state():
    """Returns (seen, dedupe): {source: set(state keys)} and
    {source: set(canon keys)}. A source missing from `seen` never ran."""
    if not os.path.exists(STATE):
        return {}, {}
    with open(STATE) as f:
        d = json.load(f)
    if isinstance(d, list):                       # v1: flat github-only list
        return {"github": set(d)}, {}
    src = d.get("sources", d)
    seen = {k: set(v) for k, v in src.items() if isinstance(v, list)}
    ded = {k: set(v) for k, v in (d.get("dedupe") or {}).items()
           if isinstance(v, list)}
    return seen, ded


def save_state(prev_seen, prev_ded, seen_up, ded_up):
    seen = {k: sorted(v) for k, v in prev_seen.items()}
    seen.update({k: sorted(v) for k, v in seen_up.items()})
    ded = {k: sorted(v) for k, v in prev_ded.items()}
    ded.update({k: sorted(v) for k, v in ded_up.items()})
    with open(STATE, "w") as f:
        json.dump({"version": 2, "sources": seen, "dedupe": ded}, f,
                  indent=0, sort_keys=True)


def load_boards():
    if not os.path.exists(BOARDS_FILE):
        return {}
    with open(BOARDS_FILE) as f:
        return json.load(f)


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------

def notify(title, body, click, priority="high"):
    if not TOPIC:
        print(f"\n--- NTFY_TOPIC unset, printing instead ---\n{title}\n\n"
              f"{body}\n")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority,
                 "Tags": "briefcase", "Click": click},
    )
    urllib.request.urlopen(req, timeout=20).read()


def announce(name, new):
    # FAANG+ first, then advanced-degree, then the rest
    new.sort(key=lambda r: (("🔥" not in r["flags"]), ("🎓" not in r["flags"])))

    companies = []
    for r in new:
        if r["company"] not in companies:
            companies.append(r["company"])
    tag = "New" if name == "github" else "New (ATS)"
    title = f"{tag}: " + ", ".join(companies[:4])
    if len(companies) > 4:
        title += f" +{len(companies) - 4} more"

    lines = []
    for r in new[:25]:
        mark = "🔥" if "🔥" in r["flags"] else ("🎓" if "🎓" in r["flags"] else "•")
        lines.append(f"{mark} {r['company']} — {r['role']}\n"
                     f"   {r['loc']}\n   {r['url']}")
    if len(new) > 25:
        lines.append(f"\n...and {len(new) - 25} more")

    notify(title, "\n\n".join(lines), new[0].get("link") or GH_LINK)


# --------------------------------------------------------------------------

def main():
    prev_seen, prev_ded = load_state()
    boards = load_boards()
    active = [n for n in PRIORITY if not ONLY or n in ONLY]

    rows_by_source, failures = {}, []
    for name in active:
        try:
            rows_by_source[name] = SOURCES[name](boards)
        except Exception as e:                       # noqa: BLE001
            print(f"[{name}] FAILED, keeping previous state: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            failures.append(name)

    # Cross-source dedupe: drop a lower-priority row whose normalised
    # company+role already appears in a higher-priority source. A source that
    # failed this run still contributes its last-known keys, so an outage
    # cannot unmask duplicates it was previously suppressing.
    claimed, seen_up, ded_up = set(), {}, {}
    for name in active:
        rows = rows_by_source.get(name)
        if rows is None:
            claimed |= prev_ded.get(name, set())
            continue
        kept, dropped = [], 0
        for r in rows:
            if r["dk"] in claimed:
                dropped += 1
                continue
            kept.append(r)
        for r in kept:
            claimed.add(r["dk"])

        seen_up[name] = {r["sk"] for r in kept}
        ded_up[name] = {r["dk"] for r in kept}
        seen = prev_seen.get(name)

        if seen is None:
            print(f"[{name}] seeded baseline with {len(seen_up[name])} "
                  f"listings, no notification sent")
            continue

        new = [r for r in kept if r["sk"] not in seen]
        extra = f", {dropped} dup of higher-priority source" if dropped else ""
        print(f"[{name}] {len(kept)} relevant{extra}, {len(new)} new")
        if new:
            announce(name, new)

    save_state(prev_seen, prev_ded, seen_up, ded_up)
    if failures:
        sys.exit(f"source(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()