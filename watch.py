#!/usr/bin/env python3
"""
Watch several sources for new internship listings and push them to ntfy.

Sources
  simplify  SimplifyJobs/Summer2027-Internships, read from the structured
            .github/scripts/listings.json rather than by scraping the README's
            HTML tables. That file carries date_posted, degrees, sponsorship,
            terms and active flags as real data, so the filters below are exact
            instead of inferred from emoji. Keyed on the listing's stable UUID,
            so retitling a posting does not re-notify.
  ats       Official company career portals, via their public APIs: Greenhouse,
            Lever, Ashby, SmartRecruiters, Workday and amazon.jobs. All
            keyless, and the origin of most postings that later show up on
            aggregators. Workday entries are configured by pasting the portal
            URL itself.

Design notes
  * State is namespaced per source. A source that errors keeps its previous
    state untouched, so a transient failure can never cause a notification
    storm on the next run.
  * A source with no state yet seeds silently. Adding or renaming a source
    therefore never floods you on its first run.
  * Rows that duplicate something in a higher-priority source are dropped,
    matched on a normalised company+role key. Dedupe keys are persisted, so an
    outage cannot unmask duplicates a source was previously suppressing.

Env
  NTFY_TOPIC   required for push; prints to stdout when unset
  STATE_FILE   default state.json
  BOARDS_FILE  default boards.json
  SOURCES      comma-separated list to restrict active sources (default: all)
  US_ONLY      "0" to keep non-US postings from the ATS source (default: on)
  MIN_YEAR     drop ATS postings naming an earlier season year (default 2027)
  TERM_YEARS   comma list of season years to accept (default 2027)
  MAX_AGE_DAYS drop listings posted more than N days ago (default: no limit)
  EXCLUDE_ACADEMIC  "0" to keep university/college employers (default: drop)

Note on filtering the simplify source: no keyword filter is applied to its
titles. Simplify's own `category` field is the better signal, and a title
filter measurably loses good postings — Jane Street, Google Student
Researcher, ByteDance, Anthropic Fellows and Blackstone Summer Analyst are all
real internships whose titles never say "intern".
"""
import concurrent.futures
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

STATE = os.environ.get("STATE_FILE", "state.json")
BOARDS_FILE = os.environ.get("BOARDS_FILE", "boards.json")
TOPIC = os.environ.get("NTFY_TOPIC")
ONLY = {s.strip() for s in os.environ.get("SOURCES", "").split(",") if s.strip()}
US_ONLY = os.environ.get("US_ONLY", "1") != "0"
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2027"))
TERM_YEARS = {y.strip() for y in os.environ.get("TERM_YEARS", "2027").split(",")
              if y.strip()}
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0")) or None
EXCLUDE_ACADEMIC = os.environ.get("EXCLUDE_ACADEMIC", "1") != "0"

# Workday paging. Its search is fuzzy and its portals are large, so poll a
# bounded window per company per run and filter titles locally.
WD_PAGES = int(os.environ.get("WD_PAGES", "10"))
WD_LIMIT = 20                      # Workday caps a page at 20
WD_QUERY = os.environ.get("WD_QUERY", "intern")
SR_MAX = int(os.environ.get("SR_MAX", "600"))
AMZN_MAX = int(os.environ.get("AMZN_MAX", "400"))

# ntfy converts any message over 4096 bytes into an attachment file, which
# arrives as "You received a file: attachment.txt" instead of readable text.
# Stay under it and link out to the digest for the rest.
NTFY_MAX = 4096
NTFY_BUDGET = int(os.environ.get("NTFY_BUDGET", "3500"))

DIGEST_FILE = os.environ.get("DIGEST_FILE", "digest.md")


def digest_url():
    """Where the full list lives. Derived from the Actions environment so it
    needs no configuration; override with DIGEST_URL."""
    explicit = os.environ.get("DIGEST_URL")
    if explicit:
        return explicit
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        branch = os.environ.get("GITHUB_REF_NAME") or "main"
        return (f"https://github.com/{repo}/blob/{branch}/"
                f"{DIGEST_FILE}")
    return GH_LINK

UA = "internship-watcher (+github actions; contact via repo issues)"

LISTINGS = ("https://raw.githubusercontent.com/SimplifyJobs/"
            "Summer2027-Internships/dev/.github/scripts/listings.json")
GH_LINK = "https://github.com/SimplifyJobs/Summer2027-Internships"

# listings.json `category`. Both the current names and the older long-form
# names are present in the file, so accept both.
CATEGORIES = {"Software", "AI/ML/Data",
              "Software Engineering", "Data Science, AI & Machine Learning"}

# Replaces the old 🛂 / 🇺🇸 emoji flags with the real field.
BAD_SPONSORSHIP = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}

# Degrees at or below Master's. A listing is kept when it names at least one of
# these, or names none at all. A listing that ONLY wants PhD/MBA/JD/MD is not
# something a Master's student can apply to, and is dropped — this is what the
# README's single 🎓 emoji could not distinguish.
DEGREES_OK = {"Bachelor's", "Master's", "Associate's", "Certificate",
              "Bootcamp", "Incomplete"}

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


def post_json(url, body, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"User-Agent": UA, "Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def age_days(epoch):
    if not epoch:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    return max(0, int((now - int(epoch)) // 86400))


def ago(days):
    if days is None:
        return ""
    if days == 0:
        return "today"
    return f"{days}d ago"


# Cross-source duplicate detection. The sources word titles differently
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
# location + relevance filters (ATS only; listings.json is pre-categorised)
# --------------------------------------------------------------------------

# CASE-SENSITIVE on purpose. Under re.I the state codes IN, OR, OK, ME, HI,
# LA, DE, CO, ID, PA, MA all match ordinary English words, so "BangPa-in,
# Thailand" was reading as Indiana and beating the "Thailand" match.
_US_ABBR = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    r"VA|WA|WV|WI|WY|DC)\b")

# US cities that commonly appear without a state qualifier.
_US_CITY = re.compile(
    r"\b(san francisco|\bSF\b|bay area|silicon valley|new york|nyc|brooklyn|"
    r"seattle|"
    r"bellevue|redmond|austin|boston|cambridge, ma|chicago|los angeles|"
    r"san jose|san diego|santa clara|sunnyvale|mountain view|menlo park|"
    r"palo alto|redwood city|foster city|san mateo|cupertino|berkeley|"
    r"oakland|emeryville|culver city|santa monica|irvine|atlanta|denver|"
    r"boulder|dallas|houston|fort worth|plano|austin|san antonio|phoenix|"
    r"tempe|scottsdale|portland|salt lake city|provo|lehi|boise|las vegas|"
    r"sacramento|miami|orlando|tampa|jacksonville|charlotte|raleigh|durham|"
    r"chapel hill|nashville|memphis|louisville|indianapolis|columbus|"
    r"cincinnati|cleveland|pittsburgh|philadelphia|baltimore|washington, d|"
    r"arlington, v|mclean|reston|herndon|richmond, v|detroit|ann arbor|"
    r"minneapolis|madison, wi|milwaukee|st\.? louis|kansas city|omaha|"
    r"des moines|new orleans|oklahoma city|tulsa|albuquerque|tucson|"
    r"el paso|hoboken|jersey city|stamford|greenwich, c|princeton|"
    r"hartford|providence|spokane|tacoma|honolulu|anchorage|malta, n|"
    r"essex junction|hillsboro|folsom|chandler|fishkill|longmont)\b", re.I)

_US_NAME = re.compile(
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
    r"costa rica|panama|iceland|reykjavik|luxembourg|malta|cyprus|morocco|"
    r"casablanca|tunisia|armenia|yerevan|kazakhstan|almaty|"
    r"emea|apac|latam)\b", re.I)

_US_COUNTRY = {"usa", "us", "u.s.", "u.s.a.", "united states",
               "united states of america"}

_INTERN = re.compile(r"\b(intern|interns|internship|internships|co-?op|"
                     r"co-?ops)\b", re.I)

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
    r"executive assistant|office manager|real estate|supply chain|mba|"
    r"logistics|warehouse|driver|technician)\b", re.I)

# An unambiguous engineering signal. It overrides _NOT_TECH, because real
# roles carry misleading suffixes: "Software Engineer Intern - Creative
# Intelligence and Brand" is a SWE job, not a brand job.
_STRONG_TECH = re.compile(
    r"\b(software|swe|backend|back-end|frontend|front-end|full.?stack|"
    r"machine learning|deep learning|data (engineer|scien|analyt)|"
    r"research (engineer|scientist)|quantitative|quant|compiler|kernel|"
    r"embedded|firmware|silicon|robotics|perception|autonomy|"
    r"(infrastructure|security|systems|platform|ml|ai|network|reliability) "
    r"engineer)\b", re.I)

# University and college employers. Their "Undergraduate Research Assistant"
# and "Student Wage" postings are categorised as Software/AI-ML upstream but
# are campus jobs, not internships.
_ACADEMIC = re.compile(
    r"(universit|\bcollege\b|\bschool\b|research foundation|polytechnic|"
    r"\bRFCUNY\b|state univ|\bacademy\b)", re.I)

_YEAR = re.compile(r"\b(20\d\d)\b")


def us_signal(t):
    return bool(_US_ABBR.search(t) or _US_NAME.search(t) or _US_CITY.search(t))


def is_us(loc, country=None):
    """Requires positive US evidence when a location is given. Workday and
    SmartRecruiters portals are global — Singapore, Eindhoven, Tianjin and
    Belo Horizonte all appear — and a permissive default floods on those.
    A blank or unknown location is still kept."""
    if country:
        c = country.strip().lower()
        if c in _US_COUNTRY:
            return True
        if len(c) > 1:              # an explicit foreign country is decisive
            return False
    t = (loc or "").strip()
    if not t:
        return True
    if us_signal(t):
        return True
    if _NON_US.search(t):
        return False
    # Bare "Remote" with no country reads as US-eligible.
    if re.fullmatch(r"(remote|remote\s*[-–]\s*\w+|anywhere)", t, re.I):
        return True
    return False


def tech(title):
    if _STRONG_TECH.search(title):
        return True
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
# source: SimplifyJobs listings.json
# --------------------------------------------------------------------------

def term_ok(terms):
    """No terms listed means unknown, which is kept. 'N/A' likewise."""
    if not terms:
        return True
    return any(t == "N/A" or any(y in t for y in TERM_YEARS) for t in terms)


def degree_ok(degrees):
    """Keep when nothing is specified, or when at least one listed degree is
    at or below Master's. Drops PhD/MBA/JD/MD-only postings."""
    if not degrees:
        return True
    return bool(set(degrees) & DEGREES_OK)


def source_simplify(_boards):
    data = fetch_json(LISTINGS, timeout=60)
    if not isinstance(data, list) or len(data) < 5000:
        raise RuntimeError(
            f"listings.json returned {len(data) if isinstance(data, list) else '?'}"
            " entries - format or path probably changed")

    out = []
    for j in data:
        if not j.get("is_visible") or not j.get("active"):
            continue                                   # replaces the 🔒 flag
        if j.get("category") not in CATEGORIES:
            continue
        if j.get("sponsorship") in BAD_SPONSORSHIP:    # replaces 🛂 / 🇺🇸
            continue
        if not term_ok(j.get("terms")):
            continue
        if not degree_ok(j.get("degrees")):
            continue
        days = age_days(j.get("date_posted"))
        if MAX_AGE_DAYS is not None and days is not None and days > MAX_AGE_DAYS:
            continue

        company = (j.get("company_name") or "?").strip()
        if EXCLUDE_ACADEMIC and _ACADEMIC.search(company):
            continue
        title = (j.get("title") or "").strip()
        locs = j.get("locations") or []
        loc = " / ".join(locs)[:60] if locs else ""
        degrees = j.get("degrees") or []
        out.append({
            "source": "simplify",
            "company": company,
            "role": title,
            "loc": loc,
            "url": j.get("url") or "",
            "age": days,
            "degrees": degrees,
            "terms": j.get("terms") or [],
            # Stable UUID: retitling a posting no longer re-notifies, and the
            # same role in two cities is two listings rather than one key.
            "sk": str(j.get("id")),
            "dk": canon(company, title),
            "link": j.get("url") or GH_LINK,
        })

    if len(out) < 50:
        raise RuntimeError(
            f"only {len(out)} listings survived filtering - check CATEGORIES "
            f"/ TERM_YEARS, refusing to run")
    print(f"[simplify] {len(data)} listings, {len(out)} relevant")
    return out


# --------------------------------------------------------------------------
# source: public ATS job boards
# --------------------------------------------------------------------------

def _label(slug, given=None):
    if given:
        return given
    s = re.sub(r"[-_]+", " ", slug).strip()
    return s.title() if s.islower() else s


def board_greenhouse(slug):
    d = fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=25)
    return [{"id": str(j.get("id")),
             "title": (j.get("title") or "").strip(),
             "loc": ((j.get("location") or {}).get("name") or "").strip(),
             "country": None,
             "url": j.get("absolute_url") or "",
             "etype": None,
             "posted": None}
            for j in d.get("jobs", [])]


def board_lever(slug):
    d = fetch_json(
        f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=25)
    out = []
    for j in d:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        out.append({"id": str(j.get("id")),
                    "title": (j.get("text") or "").strip(),
                    "loc": (cats.get("location") or "").strip(),
                    "country": None,
                    "url": j.get("hostedUrl") or "",
                    "etype": cats.get("commitment"),
                    "posted": int(created) // 1000 if created else None})
    return out


def board_ashby(slug):
    d = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=25)
    out = []
    for j in d.get("jobs", []):
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        pub = j.get("publishedAt")
        ts = None
        if pub:
            try:
                ts = int(datetime.datetime.fromisoformat(
                    pub.replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        out.append({"id": str(j.get("id")),
                    "title": (j.get("title") or "").strip(),
                    "loc": (j.get("location") or "").strip(),
                    "country": addr.get("addressCountry"),
                    "url": j.get("jobUrl") or "",
                    "etype": j.get("employmentType"),
                    "posted": ts})
    return out


def board_smartrecruiters(slug):
    out, offset = [], 0
    while offset < SR_MAX:
        d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{slug}"
                       f"/postings?limit=100&offset={offset}", timeout=25)
        page = d.get("content") or []
        if not page:
            break
        for j in page:
            loc = j.get("location") or {}
            rel = j.get("releasedDate")
            ts = None
            if rel:
                try:
                    ts = int(datetime.datetime.fromisoformat(
                        rel.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    ts = None
            out.append({
                "id": str(j.get("id")),
                "title": (j.get("name") or "").strip(),
                "loc": (loc.get("fullLocation") or loc.get("city") or "").strip(),
                "country": loc.get("country"),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                # SmartRecruiters tags the posting type explicitly.
                "etype": ((j.get("typeOfEmployment") or {}).get("id")),
                "posted": ts,
            })
        offset += len(page)
        if len(page) < 100:
            break
    return out


# Accepts a pasted portal URL — https://nvidia.wd5.myworkdayjobs.com/
# NVIDIAExternalCareerSite — or the compact "tenant|host|site" form.
_WD_URL = re.compile(
    r"^(?:https?://)?([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-zA-Z]{2}-[A-Za-z]{2}/)?([^/?#]+)", re.I)
_WD_MULTI = re.compile(r"^\d+\s+locations?$", re.I)
_WD_POSTED = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def _wd_parse(spec):
    m = _WD_URL.match(spec.strip())
    if m:
        return m.group(1).lower(), m.group(2).lower(), m.group(3)
    parts = [p for p in spec.split("|")]
    if len(parts) == 3:
        return parts[0].strip().lower(), parts[1].strip().lower(), parts[2].strip()
    raise ValueError(f"cannot parse Workday portal spec: {spec!r}")


def _wd_posted(text):
    """'Posted 11 Days Ago' -> epoch. Workday gives no real timestamp, so this
    is day-resolution only, and '30+ Days Ago' floors at 30."""
    if not text:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    low = text.lower()
    if "today" in low:
        return int(now)
    if "yesterday" in low:
        return int(now - 86400)
    m = _WD_POSTED.search(low)
    return int(now - int(m.group(1)) * 86400) if m else None


def _wd_location(j):
    """locationsText is often 'N Locations'; the externalPath slug carries a
    real place, so fall back to that."""
    text = (j.get("locationsText") or "").strip()
    if text and not _WD_MULTI.match(text):
        return text
    path = j.get("externalPath") or ""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "job":
        return parts[1].replace("---", " - ").replace("-", " ").strip()
    return text


def board_workday(spec):
    tenant, host, site = _wd_parse(spec)
    api = (f"https://{tenant}.{host}.myworkdayjobs.com"
           f"/wday/cxs/{tenant}/{site}/jobs")
    base = f"https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}"
    out, seen = [], set()
    for page in range(WD_PAGES):
        d = post_json(api, {"appliedFacets": {}, "limit": WD_LIMIT,
                            "offset": page * WD_LIMIT,
                            "searchText": WD_QUERY}, timeout=25)
        posts = d.get("jobPostings") or []
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath") or ""
            jid = (j.get("bulletFields") or [path])[0]
            if jid in seen:
                continue
            seen.add(jid)
            out.append({
                "id": str(jid),
                "title": (j.get("title") or "").strip(),
                "loc": _wd_location(j),
                "country": None,
                "url": base + path,
                "etype": None,
                "posted": _wd_posted(j.get("postedOn")),
            })
        if len(posts) < WD_LIMIT:
            break
    return out


def board_amazon(query):
    """amazon.jobs has its own search JSON. The key in boards.json is the
    search query, not a company slug. Note `is_intern` comes back as the
    string 'None' rather than a boolean, so it is unusable — the title filter
    does the work."""
    out, offset = [], 0
    while offset < AMZN_MAX:
        d = fetch_json("https://www.amazon.jobs/en/search.json"
                       f"?base_query={urllib.parse.quote(query)}"
                       f"&result_limit=100&offset={offset}&sort=recent",
                       timeout=30)
        page = d.get("jobs") or []
        if not page:
            break
        for j in page:
            ts = None
            pd = j.get("posted_date")
            if pd:
                try:
                    ts = int(datetime.datetime.strptime(pd, "%B %d, %Y")
                             .replace(tzinfo=datetime.timezone.utc).timestamp())
                except ValueError:
                    ts = None
            out.append({
                "id": str(j.get("id_icims") or j.get("id")),
                "title": (j.get("title") or "").strip(),
                "loc": (j.get("normalized_location")
                        or j.get("location") or "").strip(),
                "country": j.get("country_code"),
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "etype": None,
                "posted": ts,
            })
        offset += len(page)
        if len(page) < 100:
            break
    return out


ATS = {"greenhouse": board_greenhouse,
       "lever": board_lever,
       "ashby": board_ashby,
       "smartrecruiters": board_smartrecruiters,
       "workday": board_workday,
       "amazon": board_amazon}


def source_ats(boards):
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
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
            days = age_days(j.get("posted"))
            if MAX_AGE_DAYS is not None and days is not None \
                    and days > MAX_AGE_DAYS:
                continue
            out.append({
                "source": "ats",
                "company": company,
                "role": title,
                "loc": (j["loc"] or "")[:60],
                "url": j["url"],
                "age": days,
                "degrees": [],
                "terms": [],
                "sk": f"{kind}:{slug}:{j['id']}",
                "dk": canon(company, title),
                "link": j["url"],
            })
    print(f"[ats] {len(targets) - len(failed)} boards ok, {len(out)} relevant")
    return out


SOURCES = {"simplify": source_simplify, "ats": source_ats}
PRIORITY = ["simplify", "ats"]        # earlier sources win a duplicate
RETIRED = {"github"}                  # old source names to prune from state


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
    if isinstance(d, list):                       # v1: flat README-only list
        return {"github": set(d)}, {}
    src = d.get("sources", d)
    seen = {k: set(v) for k, v in src.items() if isinstance(v, list)}
    ded = {k: set(v) for k, v in (d.get("dedupe") or {}).items()
           if isinstance(v, list)}
    return seen, ded


def save_state(prev_seen, prev_ded, seen_up, ded_up):
    seen = {k: sorted(v) for k, v in prev_seen.items() if k not in RETIRED}
    seen.update({k: sorted(v) for k, v in seen_up.items()})
    ded = {k: sorted(v) for k, v in prev_ded.items() if k not in RETIRED}
    ded.update({k: sorted(v) for k, v in ded_up.items()})
    with open(STATE, "w") as f:
        json.dump({"version": 3, "sources": seen, "dedupe": ded}, f,
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


def entry_lines(r):
    meta = ago(r["age"])
    # Surface a degree ceiling the title does not mention.
    if r["degrees"] and "Bachelor's" not in r["degrees"]:
        meta += (" · " if meta else "") + "/".join(r["degrees"])
    return (f"• {r['company']} — {r['role']}\n   {r['loc']}"
            + (f"   [{meta}]" if meta else "")
            + f"\n   {r['url']}")


def announce(name, new, link):
    # Newest posting first; unknown age last.
    new.sort(key=lambda r: (r["age"] is None, r["age"] if r["age"] else 0))

    companies = []
    for r in new:
        if r["company"] not in companies:
            companies.append(r["company"])
    tag = "New" if name == "simplify" else "New (ATS)"
    title = f"{tag}: " + ", ".join(companies[:4])
    if len(companies) > 4:
        title += f" +{len(companies) - 4} more"

    # Fill up to the byte budget rather than a fixed entry count: entries vary
    # from ~90 to ~300 bytes, so a count-based cap cannot bound the message.
    lines, used, shown = [], 0, 0
    for r in new:
        block = entry_lines(r)
        cost = len(block.encode("utf-8")) + 2
        if used + cost > NTFY_BUDGET:
            break
        lines.append(block)
        used += cost
        shown += 1

    if shown < len(new):
        lines.append(f"…and {len(new) - shown} more — full list:\n{link}")
    else:
        lines.append(f"Full list: {link}")

    body = "\n\n".join(lines)
    if len(body.encode("utf-8")) > NTFY_MAX:          # belt and braces
        body = body.encode("utf-8")[:NTFY_MAX - 200].decode("utf-8", "ignore")
        body += f"\n\n…truncated — full list:\n{link}"
    notify(title, body, link)


def write_digest(new_by_source, boards, path=DIGEST_FILE):
    """The page the notification's Click opens: every new posting in full,
    then the complete watched-company index with portal links."""
    now = datetime.datetime.now(datetime.timezone.utc)
    total = sum(len(v) for v in new_by_source.values())
    out = ["# Internship watcher — latest",
           "",
           f"_Updated {now:%Y-%m-%d %H:%M UTC} · {total} new listing"
           f"{'' if total == 1 else 's'}_",
           ""]

    if not total:
        out += ["Nothing new this run.", ""]
    for src in PRIORITY:
        rows = new_by_source.get(src) or []
        if not rows:
            continue
        label = "SimplifyJobs" if src == "simplify" else "Company portals"
        out += [f"## New from {label} ({len(rows)})", "",
                "| Company | Role | Location | Posted | Link |",
                "|---|---|---|---|---|"]
        for r in rows:
            role = (r["role"] or "").replace("|", "\\|")
            comp = (r["company"] or "").replace("|", "\\|")
            loc = (r["loc"] or "—").replace("|", "\\|")
            deg = ("<br>" + "/".join(r["degrees"])
                   if r["degrees"] and "Bachelor's" not in r["degrees"] else "")
            out.append(f"| **{comp}** | {role}{deg} | {loc} | "
                       f"{ago(r['age']) or '—'} | [apply]({r['url']}) |")
        out.append("")

    out += ["## Watched company portals", "",
            "Every board polled each run, with the page to check by hand.", ""]
    linkers = {
        "greenhouse": lambda s: f"https://job-boards.greenhouse.io/{s}",
        "lever": lambda s: f"https://jobs.lever.co/{s}",
        "ashby": lambda s: f"https://jobs.ashbyhq.com/{s}",
        "smartrecruiters": lambda s: f"https://jobs.smartrecruiters.com/{s}",
        "workday": lambda s: s if s.startswith("http") else "#",
        "amazon": lambda s: ("https://www.amazon.jobs/en/search?base_query="
                             + urllib.parse.quote(s)),
    }
    rows = []
    for kind, entry in (boards or {}).items():
        if kind not in linkers or not isinstance(entry, dict):
            continue
        for slug, nm in entry.items():
            rows.append((nm, kind, linkers[kind](slug)))
    out += [f"_{len(rows)} boards_", "",
            "| Company | Platform | Careers page |", "|---|---|---|"]
    for nm, kind, url in sorted(rows, key=lambda x: x[0].lower()):
        out.append(f"| {nm} | {kind} | [open]({url}) |")
    out.append("")

    with open(path, "w") as f:
        f.write("\n".join(out))
    return path


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

    # Cross-source dedupe. A source that failed this run still contributes its
    # last-known keys, so an outage cannot unmask duplicates it was
    # previously suppressing.
    claimed, seen_up, ded_up, new_by_source = set(), {}, {}, {}
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
        new.sort(key=lambda r: (r["age"] is None, r["age"] if r["age"] else 0))
        new_by_source[name] = new
        extra = f", {dropped} dup of higher-priority source" if dropped else ""
        print(f"[{name}] {len(kept)} relevant{extra}, {len(new)} new")

    # Written before notifying so the link the push opens is already correct.
    write_digest(new_by_source, boards)
    link = digest_url()
    for name in active:
        if new_by_source.get(name):
            announce(name, new_by_source[name], link)

    save_state(prev_seen, prev_ded, seen_up, ded_up)
    if failures:
        sys.exit(f"source(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()