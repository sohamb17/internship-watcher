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
  SEASONS      terms to accept (default "Summer 2027,Fall 2027"); listings
               naming no term are kept
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
import html
import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

STATE = os.environ.get("STATE_FILE", "state.json")
BOARDS_FILE = os.environ.get("BOARDS_FILE", "boards.json")
PRIORITY_FILE = os.environ.get("PRIORITY_FILE", "priority.json")
TOPIC = os.environ.get("NTFY_TOPIC")
ONLY = {s.strip() for s in os.environ.get("SOURCES", "").split(",") if s.strip()}
US_ONLY = os.environ.get("US_ONLY", "1") != "0"
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2027"))
# Recruiting terms you want. A listing that names its term is kept only if it
# names one of these; a listing that names no term at all is kept (most Summer
# postings never say). "Winter 2027" / "Spring 2027" are January-start co-op
# terms, a different cycle from Summer.
SEASONS = [t.strip() for t in os.environ.get("SEASONS",
                                             "Summer 2027,Fall 2027").split(",")
           if t.strip()]
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
# Quiet hours, local time, "start-end" in 24h hours ("0-8" = midnight to
# 8 am). Listings found then are held and sent as one summary on the first
# run after quiet hours end; tier-1 companies (priority.json) still alert.
# Empty disables.
QUIET_HOURS = os.environ.get("QUIET_HOURS", "0-8")
QUIET_TZ = os.environ.get("QUIET_TZ", "America/Chicago")
QUIET_BREAKTHROUGH_TIER = int(os.environ.get("QUIET_BREAKTHROUGH_TIER", "1"))
HELD_MAX_DAYS = 3                  # a held row that can't be sent is dropped


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
VANSH_LISTINGS = ("https://raw.githubusercontent.com/vanshb03/"
                  "Summer2027-Internships/dev/.github/scripts/listings.json")
RAW = "https://raw.githubusercontent.com/"
SPEEDY_README = RAW + "speedyapply/2027-SWE-College-Jobs/refs/heads/main/README.md"
SNDSH_README = RAW + "sndsh404/summer-2027-internships/refs/heads/main/README.md"
ZAPPLY_README = RAW + "zapplyjobs/Internships-2027/refs/heads/main/README.md"
JOBRIGHT_README = (RAW + "jobright-ai/2026-Software-Engineer-Internship/"
                   "refs/heads/master/README.md")   # rolling list; carries 2027 roles
NOINTERNSHIP_URL = "https://www.nointernship.com/internships"
ZSHAH_JSON = (RAW + "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-"
              "Tech-Internships/refs/heads/main/data/jobs.json")

# Memory. A listing alerts only if neither its ID nor its normalised
# company+role has been observed within this many days, so a posting that
# drops out of one run and returns, or moves between sources, is not new.
SEEN_TTL_DAYS = int(os.environ.get("SEEN_TTL_DAYS", "60"))
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


# Second-pass duplicate check, same company only. canon() catches identical
# wording; different sources word one role differently:
#   "Embedded Systems Engineer Intern" / "Embedded Systems Engineering Intern"
#   "CDS Intern - Software Engineer - IT Retail Media" /
#       "Software Engineer Intern, IT Retail Media"
#   "2027 Summer Intern, BS/MS, Software Engineering, Commercialization" /
#       "Software Engineering Intern - Commercialization"
#   "Summer 2027 Intern - Embedded Firmware - 5810" / "Embedded Firmware Intern"
# Titles are reduced to their meaningful words (degree tags, job numbers, work
# mode and the company's own name removed; engineering -> engineer) and are
# the same role when the word sets match, or when one contains the other and
# the only extra words are short codes like "CDS". Short meaningful words
# (AI, ML, iOS, SRE...) count as a real difference, and a title needs three
# words before containment applies, so "Software Engineer Intern" and
# "Software Engineer Intern, Backend" stay distinct.
_FUZZ_DROP = {"bs", "ms", "ba", "ma", "phd", "mba", "bsms", "msphd", "student",
              "graduate", "undergraduate", "onsite", "hybrid", "remote", "in",
              "on", "site", "level", "i", "ii", "iii"}
_FUZZ_STEM = {"engineering": "engineer", "engineers": "engineer",
              "developers": "developer", "analysts": "analyst",
              "scientists": "scientist", "systems": "system",
              "services": "service", "solutions": "solution",
              "technologies": "technology", "operations": "operation"}
_FUZZ_MEANINGFUL = {"ai", "ml", "ar", "vr", "xr", "ios", "qa", "ui", "ux",
                    "sre", "gpu", "cpu", "soc", "rf", "it", "hr", "pm", "llm",
                    "nlp", "cv", "api", "sql", "web", "app", "c", "go", "dev",
                    "ops", "net", "mac", "fpga", "asic", "rtl", "pcb", "hw",
                    "sw", "os", "db", "bi", "erp", "crm", "sap", "seo"}


def role_words(dk):
    """canon() key -> (company, frozenset of meaningful title words)."""
    company, _, words = dk.partition("|")
    out = set()
    for t in words.split():
        if t.isdigit() or t in _FUZZ_DROP:
            continue
        if t == company or (len(t) >= 5 and company.startswith(t)):
            continue                      # "Costco Digital Services Intern"
        out.add(_FUZZ_STEM.get(t, t))
    return company, frozenset(out)


def same_role(a, b):
    if a == b:
        return bool(a)
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return (len(small) >= 3 and small <= big
            and all(len(t) <= 3 and t not in _FUZZ_MEANINGFUL for t in big - small))


def same_company(a, b):
    """Sources disagree on company names: "Shield" / "Shield AI", "Costco" /
    "Costco Wholesale". Treat one name being the start of the other as the
    same company, if the shorter is at least 6 letters (so "Intel" does not
    swallow "Intelligent Systems")."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 6 and long_.startswith(short)


class RoleIndex:
    """Per-company word sets for the fuzzy check, bucketed by the first six
    letters of the company so related names are compared cheaply."""

    def __init__(self, keys=()):
        self.by_co = {}
        self.bucket = {}
        for k in keys:
            self.add(k)

    def add(self, dk):
        co, words = role_words(dk)
        if co not in self.by_co:
            self.by_co[co] = set()
            self.bucket.setdefault(co[:6], set()).add(co)
        self.by_co[co].add(words)

    def match(self, dk):
        co, words = role_words(dk)
        for other in self.bucket.get(co[:6], ()):
            if same_company(co, other) and any(
                    same_role(words, w) for w in self.by_co[other]):
                return True
        return False


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
    r"robotics|autonomy|perception|graphics|simulation|technical|technology|"
    r"app dev|application develop)\b", re.I)

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
    r"product manag(er|ement)|program manag(er|ement)|"
    r"designer|partnerships|procurement|facilities|workplace|"
    r"executive assistant|office manager|real estate|supply chain|mba|"
    r"logistics|warehouse|driver|technician|"
    # Engineering disciplines that aren't software. Polling whole company
    # boards (an engineering firm like HNTB lists 170+ "Intern Engineer"
    # roles) exposed these; a software signal in the title still overrides.
    r"civil|structural|structures|geotechnical|transportation|construction|"
    r"mechanical|aeromechanical|aerospace engineer|manufacturing|"
    r"process engineer|process integration|chemical|environmental|"
    r"water resources|hvac|plumbing|mep|electrical engineer|"
    r"electrical engineering|quality engineer|quality engineering|"
    r"materials engineer|noise|vibration|"
    r"packaging|sports science|motorsport|field service)\b", re.I)

# An unambiguous engineering signal. It overrides _NOT_TECH, because real
# roles carry misleading suffixes: "Software Engineer Intern - Creative
# Intelligence and Brand" is a SWE job, not a brand job.
_STRONG_TECH = re.compile(
    r"\b(software|swe|backend|back-end|frontend|front-end|full.?stack|"
    r"machine learning|deep learning|data (engineer|scien|analyt)|"
    r"research (engineer|scientist)|quantitative|quant|compiler|kernel|"
    r"embedded|firmware|silicon|robotics|perception|autonomy|"
    r"(infrastructure|security|systems|platform|ml|ai|network|reliability) "
    r"engineer|mobile|ios|android|gameplay|game engine|"
    r"member of technical staff|motion planning|business intelligence)\b", re.I)

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


_VAGUE = re.compile(
    r"\b(intern|interns|internship|internships|co-?op|coop|engineer|"
    r"engineers|engineering|summer|fall|winter|spring|20\d\d|returning|"
    r"student|students|program|programme|the|and|of|for|in|us|usa)\b", re.I)


# Full-time roles that mention interns only as eligibility:
# "New Grad Engineer I (For Current & Recent HNTB Interns Only)".
_NOT_INTERN = re.compile(
    r"\bnew[- ]?grad|\bnew graduate|\bentry[- ]level\b|"
    r"\bfor (current|recent|former|returning)\b[^)]*\binterns?\b", re.I)


def tech(title):
    if _NOT_INTERN.search(title):
        return False
    if _STRONG_TECH.search(title):
        return True
    # A title made only of generic words ("Intern Engineer - Summer 2027",
    # "Engineering Intern") can't be judged; from a whole-company board such
    # as an engineering firm's, it is almost always not software.
    if not re.sub(r"[^a-z]+", "", _VAGUE.sub(" ", title).lower()):
        return False
    return bool(_TECH.search(title)) and not _NOT_TECH.search(title)


_SEASON_NAMES = {"summer": "summer", "fall": "fall", "autumn": "fall",
                 "winter": "winter", "spring": "spring"}
# Months -> the term an internship starting then belongs to.
_MONTH_TERM = {"jan": "spring", "feb": "spring", "mar": "spring",
               "apr": "spring", "may": "summer", "jun": "summer",
               "jul": "summer", "aug": "fall", "sep": "fall", "oct": "fall",
               "nov": "fall", "dec": "winter"}
# Year: four digits, or two after an apostrophe ("Summer '27"). A bare two
# digits would read "Fall 12-week program" as Fall 2012.
_TERM_RX = re.compile(
    r"\b(summer|fall|autumn|winter|spring)\b(?:\s*[-/]?\s*(?:(20\d\d)|'(\d\d))\b)?",
    re.I)
# Co-op start dates: "Co-op - January 2027", "(Jan - Jun 2027)", "Jan 2027".
# Whole month names only, so "Marketing 2027" is not March 2027.
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)")
_MONTH_RX = re.compile(
    r"\b" + _MON + r"\b\.?(?:\s*[-–to]+\s*" + _MON + r"\b\.?)?,?\s*(20\d\d)\b",
    re.I)


def _allowed():
    out = set()
    for t in SEASONS:
        m = _TERM_RX.search(t)
        if m:
            y = m.group(2) or (m.group(3) and "20" + m.group(3))
            out.add((_SEASON_NAMES[m.group(1).lower()], y or None))
    return out


ALLOWED_TERMS = _allowed()


def _term_allowed(season, year):
    return any(season == s and (year is None or y is None or year == y)
               for s, y in ALLOWED_TERMS)


# "Grad Year: Spring 2027", "graduating Winter 2027", "class of Fall 2027"
# describe the applicant, not the internship term.
_GRAD_PHRASE = re.compile(
    r"\b(grad(uat(e|ion|ing))?\s*(year|date|term)?|class of|expected graduation"
    r"|graduating)\s*[:\-]?\s*(in\s+)?(summer|fall|autumn|winter|spring)?"
    r"\s*(20\d\d)?", re.I)


_YEAR4 = re.compile(r"\b(20\d\d)\b")
_MONTH_YEAR_RANGE = re.compile(
    r"(\b" + _MON + r"\b\.?,?\s*20\d\d)\s*(?:-|–|to)\s*" + _MON + r"\b\.?,?\s*20\d\d",
    re.I)


def terms_in(text):
    """(season, year-or-None) pairs named in free text.

    A season without its own year takes the nearest year written after it,
    else before it: "Fall/Winter 2026" is Fall 2026 and Winter 2026, and
    "2026 Internship, Fall" is Fall 2026. A dated range ("Sept 2026 - May
    2027") is named by its start."""
    text = _GRAD_PHRASE.sub(" ", text or "")
    text = _MONTH_YEAR_RANGE.sub(lambda m: m.group(1), text)
    years = [(m.start(), m.group(1)) for m in _YEAR4.finditer(text)]

    def nearest(pos):
        after = [y for p, y in years if p > pos]
        before = [y for p, y in years if p < pos]
        return after[0] if after else (before[-1] if before else None)

    found = []
    for m in _TERM_RX.finditer(text):
        y = m.group(2) or (m.group(3) and "20" + m.group(3)) or nearest(m.start())
        found.append((_SEASON_NAMES[m.group(1).lower()], y))
    for m in _MONTH_RX.finditer(text):
        # "Jan - Jun 2027" is named by its start month.
        found.append((_MONTH_TERM[m.group(1)[:3].lower()], m.group(3)))
    return found


def season_ok(*texts):
    """True unless the listing names its term(s) and none is wanted. Each
    argument is a title or a structured season/term string; "N/A", "Not
    stated" and the like name nothing and so never reject."""
    found = [t for x in texts for t in terms_in(x)]
    return not found or any(_term_allowed(s, y) for s, y in found)


def relevant(title, loc, country=None):
    if not _INTERN.search(title) or not tech(title):
        return False
    years = [int(y) for y in _YEAR.findall(title)]
    if years and max(years) < MIN_YEAR:
        return False
    if not season_ok(title):
        return False
    if US_ONLY and not is_us(loc, country):
        return False
    return True


# --------------------------------------------------------------------------
# source: SimplifyJobs listings.json
# --------------------------------------------------------------------------

def term_ok(terms):
    """Simplify's `terms` list. Empty or ['N/A'] means unstated: kept."""
    return season_ok(*(terms or []))


def degree_ok(degrees):
    """Keep when nothing is specified, or when at least one listed degree is
    at or below Master's. Drops PhD/MBA/JD/MD-only postings."""
    if not degrees:
        return True
    return bool(set(degrees) & DEGREES_OK)


def _loc_ok(locs):
    """US filter over a list of locations: keep if any one is US, or if none
    are given. Before this, only the company-portal source was US-filtered,
    which is how London and Birmingham Goldman roles reached you."""
    if not US_ONLY or not locs:
        return True
    real = [l for l in locs if not re.fullmatch(r"\s*remote\s*", l, re.I)]
    if real:
        # "Remote" next to an explicit place inherits that place:
        # ["Remote", "Canada"] is remote-in-Canada, not US.
        return any(is_us(l) for l in real)
    return True


def _listings_source(name, url, min_entries, curated):
    """Reader for the SimplifyJobs listings.json schema, shared by Simplify
    and vanshb03. `curated` means the file has a reliable `category` (and
    degrees/terms); vanshb03's has none, so its titles are filtered instead."""
    data = fetch_json(url, timeout=60)
    if not isinstance(data, list) or len(data) < min_entries:
        raise RuntimeError(
            f"listings.json returned "
            f"{len(data) if isinstance(data, list) else '?'} entries - "
            "format or path probably changed")
    out = []
    for j in data:
        if not j.get("is_visible", True) or not j.get("active"):
            continue
        title = (j.get("title") or "").strip()
        if curated:
            if j.get("category") not in CATEGORIES:
                continue
            if not term_ok(j.get("terms")) or not degree_ok(j.get("degrees")):
                continue
        else:
            if not tech(title):
                continue
            years = [int(y) for y in _YEAR.findall(title)]
            if years and max(years) < MIN_YEAR:
                continue
            # vanshb03's `season` is a bare name ("Winter", "Spring/Summer")
            # for this repo's 2027 cycle; titles usually omit it entirely.
            season = j.get("season") or ""
            if season and not season_ok(season, title):
                continue
        if not season_ok(title):                      # title, every source
            continue
        if j.get("sponsorship") in BAD_SPONSORSHIP:
            continue
        locs = j.get("locations") or []
        if not _loc_ok(locs):
            continue
        days = age_days(j.get("date_posted"))
        if MAX_AGE_DAYS is not None and days is not None and days > MAX_AGE_DAYS:
            continue
        company = (j.get("company_name") or "?").strip()
        if EXCLUDE_ACADEMIC and _ACADEMIC.search(company):
            continue
        out.append({
            "source": name, "company": company, "role": title,
            "loc": " / ".join(locs)[:60] if locs else "",
            "url": j.get("url") or "", "age": days,
            "degrees": j.get("degrees") or [], "terms": j.get("terms") or [],
            "sk": str(j.get("id")), "dk": canon(company, title),
            "link": j.get("url") or GH_LINK,
        })
    if len(out) < 20:
        raise RuntimeError(f"only {len(out)} listings survived filtering - "
                           "refusing to run")
    print(f"[{name}] {len(data)} listings, {len(out)} relevant")
    return out


def source_simplify(_boards):
    return _listings_source("simplify", LISTINGS, 5000, curated=True)


def source_vansh(_boards):
    return _listings_source("vansh", VANSH_LISTINGS, 1000, curated=False)


_TRACKING = re.compile(r"^(utm_\w+|ref|s|source)$", re.I)
_TAG = re.compile(r"<[^>]+>")
_MDLINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_HREF = re.compile(r'href="([^"]+)"')
_AGE = re.compile(r"(\d+)\s*(mo|m|h|d|w)\b", re.I)
_FLAGS_CLOSED = "\U0001F512"                       # 🔒
_FLAGS_NOVISA = ("\U0001F6C2", "\U0001F1FA\U0001F1F8")  # 🛂 🇺🇸


def _clean_url(u):
    """Stable key from a URL: drop tracking params (utm_*, ref, s) but keep
    real ones such as Greenhouse's gh_jid."""
    pr = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(pr.query)
         if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit((pr.scheme, pr.netloc.lower(),
                                    pr.path.rstrip("/"),
                                    urllib.parse.urlencode(q), ""))


def _text(cell):
    """Markdown/HTML table cell -> plain text."""
    cell = _MDLINK.sub(lambda m: m.group(1), cell)
    return html.unescape(_TAG.sub("", cell)).replace("**", "").strip()


def _age_text(t):
    """'4d', '3h', '12m' (minutes), '2w', '1mo' -> days. None if unknown."""
    m = _AGE.search(t or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    return {"m": 0, "h": 0, "d": n, "w": 7 * n, "mo": 30 * n}[u] if u != "d" else n


def _table_rows(txt, start=None):
    """Data rows of the pipe tables in `txt` (optionally only after the
    heading line `start`, up to the next '## ' heading), split into cells."""
    lines = txt.splitlines()
    if start is not None:
        i = next((k for k, l in enumerate(lines) if l.startswith(start)), None)
        if i is None:
            return []
        j = next((k for k in range(i + 1, len(lines))
                  if lines[k].startswith("## ")), len(lines))
        lines = lines[i:j]
    out, last = [], None
    for l in lines:
        if not l.startswith("|"):
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if not cells or cells[0].lower() in ("company", "**company**") \
                or set("".join(cells)) <= set("-: "):
            continue
        # "↳" (or a blank first cell) means "same company as the row above".
        if last is not None and (not _text(cells[0]) or _text(cells[0]).startswith("↳")):
            cells[0] = last
        else:
            last = cells[0]
        out.append(cells)
    return out


def _row(src, co, role, loc, url, age, sk, city_only=False):
    """Common post-filter for the table sources, which carry no category:
    technical title, season year, US location, not a campus job."""
    if not co or not role or not url:
        return None
    if not tech(role):
        return None
    years = [int(y) for y in _YEAR.findall(role)]
    if years and max(years) < MIN_YEAR:
        return None
    if not season_ok(role):
        return None
    if EXCLUDE_ACADEMIC and _ACADEMIC.search(co):
        return None
    if US_ONLY:
        # A source that gives bare cities ("Radnor", "Topeka") can't supply
        # positive US evidence, so there only a clear foreign signal rejects.
        if (_NON_US.search(loc) and not us_signal(loc)) if city_only \
                else not is_us(loc):
            return None
    if MAX_AGE_DAYS is not None and age is not None and age > MAX_AGE_DAYS:
        return None
    return {"source": src, "company": co, "role": role, "loc": loc[:60],
            "url": url, "age": age, "degrees": [], "terms": [],
            "sk": sk or _clean_url(url), "dk": canon(co, role), "link": url}


def _finish(name, raw, rows, minimum):
    if raw < minimum:
        raise RuntimeError(f"only parsed {raw} rows - format probably changed")
    rows = [r for r in rows if r]
    print(f"[{name}] {raw} rows, {len(rows)} relevant")
    return rows


def source_speedy(_boards):
    """speedyapply/2027-SWE-College-Jobs, USA internship section only (it
    also lists international roles). Tables have Salary in some sections and
    not others, so columns are taken from the ends of the row."""
    txt = fetch(SPEEDY_README, timeout=60).decode("utf-8", "replace")
    raw = _table_rows(txt, start="## 2027 USA SWE Internships")
    out = []
    for c in raw:
        if len(c) < 5:
            continue
        m = _HREF.search(c[-2])
        out.append(_row("speedy", _text(c[0]), _text(c[1]), _text(c[2]),
                        m.group(1) if m else "", _age_text(c[-1]), None))
    return _finish("speedy", len(raw), out, 100)


def source_sndsh(_boards):
    """sndsh404/summer-2027-internships. Small, hand-curated. Its legend marks
    🔒 closed, 🛂 no sponsorship, 🇺🇸 citizens only — all three are dropped."""
    txt = fetch(SNDSH_README, timeout=60).decode("utf-8", "replace")
    raw = _table_rows(txt, start="## the list")
    out = []
    today = datetime.date.today()
    for c in raw:
        if len(c) < 5:
            continue
        line = " ".join(c)
        if _FLAGS_CLOSED in line or any(f in line for f in _FLAGS_NOVISA):
            continue
        m = _MDLINK.search(c[3])
        try:
            age = (today - datetime.date.fromisoformat(c[4][:10])).days
        except ValueError:
            age = None
        out.append(_row("sndsh", _text(c[0]), _text(c[1]), _text(c[2]),
                        m.group(2) if m else "", age, None))
    return _finish("sndsh", len(raw), out, 30)


def source_zapply(_boards):
    """zapplyjobs/Internships-2027. Posted is '12m' / '3h' / '2d' / '1w'."""
    txt = fetch(ZAPPLY_README, timeout=60).decode("utf-8", "replace")
    raw = [c for c in _table_rows(txt) if c[0].startswith("**") and len(c) >= 6]
    out = []
    for c in raw:
        links = _MDLINK.findall(c[-1]) or [("", "")]
        out.append(_row("zapply", _text(c[0]), _text(c[1]), _text(c[2]),
                        links[-1][1], _age_text(c[3]), None))
    return _finish("zapply", len(raw), out, 100)


_JOBRIGHT_ID = re.compile(r"/jobs/info/([0-9a-f]+)")


def source_jobright(_boards):
    """jobright-ai's rolling SWE intern list (the repo is named 2026 but is
    live and posts 2027 roles). Dates are 'Nov 02' with no year."""
    txt = fetch(JOBRIGHT_README, timeout=60).decode("utf-8", "replace")
    raw = [c for c in _table_rows(txt) if len(c) >= 5 and "jobright.ai" in c[1]]
    out = []
    today = datetime.date.today()
    for c in raw:
        links = _MDLINK.findall(c[1])
        url = links[0][1] if links else ""
        age = None
        try:
            d = datetime.datetime.strptime(f"{c[-1]} {today.year}", "%b %d %Y").date()
            if d > today:                              # Dec listing read in Jan
                d = d.replace(year=today.year - 1)
            age = (today - d).days
        except ValueError:
            pass
        m = _JOBRIGHT_ID.search(url)
        out.append(_row("jobright", _text(c[0]), _text(c[1]), _text(c[2]), url,
                        age, f"jobright:{m.group(1)}" if m else None))
    return _finish("jobright", len(raw), out, 20)


_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_NI_ID = re.compile(r"/internships/(\d+)")
_NI_CARD = re.compile(r'href="/internships/(\d+)"')


def source_nointernship(_boards):
    """nointernship.com. Its robots.txt allows /internships and disallows
    /api/, so this reads only the public page, once per run.

    The page publishes a schema.org ItemList (JSON-LD) of the 25 newest
    listings: stable numeric ID, "Title at Company", sorted newest first.
    Polling every 30 minutes, the newest 25 is the slice that matters. Each
    listing's HTML card supplies its location and apply link; cards are
    joined to the list by ID."""
    page = fetch(NOINTERNSHIP_URL, timeout=30).decode("utf-8", "replace")
    items = []
    for blk in _LDJSON.findall(page):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "ItemList":
            items = d.get("itemListElement") or []
    if len(items) < 5:
        raise RuntimeError(f"ItemList has {len(items)} items - page format "
                           "probably changed")
    # HTML segment per ID: from its first card link to the next listing's.
    # Card links only — the JSON-LD block mentions every ID too.
    marks = []
    for m in _NI_CARD.finditer(page):
        if not marks or marks[-1][0] != m.group(1):
            if m.group(1) not in {x[0] for x in marks}:
                marks.append((m.group(1), m.start()))
    seg = {}
    for k, (lid, pos) in enumerate(marks):
        end = marks[k + 1][1] if k + 1 < len(marks) else len(page)
        seg[lid] = page[pos:end]
    out = []
    for it in items:
        url = it.get("url") or ""
        m = _NI_ID.search(url)
        name = it.get("name") or ""
        if not m or " at " not in name:
            continue
        lid = m.group(1)
        role, co = (x.strip() for x in name.rsplit(" at ", 1))
        body = seg.get(lid, "")
        apply = next((h for h in _HREF.findall(body)
                      if h.startswith("http") and "nointernship.com" not in h),
                     url)
        # The 📍 pin and the city are separate text nodes, so the city is
        # usually on the line after the pin.
        lines = [l.strip() for l in html.unescape(_TAG.sub("\n", body)).splitlines()
                 if l.strip()]
        loc = ""
        for k, l in enumerate(lines):
            if "\U0001F4CD" in l:
                loc = l.replace("\U0001F4CD", "").strip() or \
                    (lines[k + 1] if k + 1 < len(lines) else "")
                break
        out.append(_row("nointernship", co, role, loc, html.unescape(apply),
                        None, f"nointernship:{lid}", city_only=True))
    return _finish("nointernship", len(items), out, 5)


ZSHAH_CATEGORIES = {"Software", "Data & ML/AI"}
ZSHAH_NO_VISA = {"citizens-only", "no-sponsorship"}


def source_zshah(_boards):
    """zshah101's automated list. Structured JSON with is_open, category and a
    sponsorship field; citizens-only and no-sponsorship roles are dropped."""
    data = fetch_json(ZSHAH_JSON, timeout=60)
    if not isinstance(data, dict) or len(data) < 300:
        raise RuntimeError("jobs.json format probably changed")
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for j in data.values():
        if not j.get("is_open") or j.get("category") not in ZSHAH_CATEGORIES:
            continue
        if j.get("sponsorship") in ZSHAH_NO_VISA:
            continue
        season = j.get("season") or ""
        seasons = j.get("seasons") or []
        if not season_ok(season, *seasons, j.get("title") or ""):
            continue
        co, role, loc = j.get("company") or "", j.get("title") or "", j.get("location") or ""
        if EXCLUDE_ACADEMIC and _ACADEMIC.search(co):
            continue
        if US_ONLY and not is_us(loc):
            continue
        age = None
        try:
            age = (now - datetime.datetime.fromisoformat(
                j["posted_at"].replace("Z", "+00:00"))).days
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        if MAX_AGE_DAYS is not None and age is not None and age > MAX_AGE_DAYS:
            continue
        out.append({"source": "zshah", "company": co, "role": role,
                    "loc": loc[:60], "url": j.get("url") or "", "age": age,
                    "degrees": [], "terms": [],
                    "sk": f"zshah:{j.get('id')}", "dk": canon(co, role),
                    "link": j.get("url") or ""})
    print(f"[zshah] {len(data)} jobs, {len(out)} relevant")
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
    # Big boards (Anduril, SpaceX) intermittently take longer than 25 s; one
    # retry with a longer timeout keeps them from being skipped.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        d = fetch_json(url, timeout=30)
    except (TimeoutError, OSError):
        d = fetch_json(url, timeout=60)
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


# Workday tenants share a handful of hosts (wd1, wd3, wd5...), and hitting one
# host with many tenants in parallel draws HTTP 429. Cap concurrency per host
# and back off on 429 rather than failing the board.
WD_PER_HOST = int(os.environ.get("WD_PER_HOST", "5"))
_wd_gates, _wd_gates_lock = {}, threading.Lock()


def _wd_post(host, url, body):
    with _wd_gates_lock:
        gate = _wd_gates.setdefault(host, threading.Semaphore(WD_PER_HOST))
    for attempt in range(4):
        with gate:
            try:
                return post_json(url, body, timeout=25)
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    raise
                wait = e.headers.get("Retry-After")
                wait = float(wait) if wait and wait.isdigit() else 2 ** attempt * 1.5
        time.sleep(min(wait, 15))


def board_workday(spec):
    tenant, host, site = _wd_parse(spec)
    api = (f"https://{tenant}.{host}.myworkdayjobs.com"
           f"/wday/cxs/{tenant}/{site}/jobs")
    base = f"https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}"
    out, seen = [], set()
    for page in range(WD_PAGES):
        d = _wd_post(host, api, {"appliedFacets": {}, "limit": WD_LIMIT,
                                 "offset": page * WD_LIMIT,
                                 "searchText": WD_QUERY})
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


ATS_OK_BOARDS = set()


def source_ats(boards):
    targets = []
    for kind, entry in boards.items():
        if kind not in ATS:
            continue
        if isinstance(entry, dict):
            targets += [(kind, slug, name) for slug, name in entry.items()]
        else:
            targets += [(kind, slug, None) for slug in entry]
    # Board URLs and slugs are case-insensitive on every platform here
    # ("NVIDIAExternalCareerSite" and "nvidiaexternalcareersite" are one
    # board), so poll each once.
    uniq, seen_keys = [], set()
    for t in targets:
        key = (t[0], t[1].lower().rstrip("/"))
        if key not in seen_keys:
            seen_keys.add(key)
            uniq.append(t)
    targets = uniq
    if not targets:
        raise RuntimeError("no ATS boards configured")

    def one(t):
        kind, slug, name = t
        try:
            return kind, slug, name, ATS[kind](slug), None
        except Exception as e:                       # noqa: BLE001
            return kind, slug, name, [], f"{type(e).__name__}: {e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(one, targets))

    failed = [f"{k}/{s} ({e})" for k, s, _, _, e in results if e]
    # Every board that answered, whether or not it has a relevant listing
    # today, so its first internship later is recognised as news.
    ATS_OK_BOARDS.clear()
    ATS_OK_BOARDS.update(f"{k}:{s}" for k, s, _, _, e in results if not e)
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
                    or (intern_type and tech(title) and season_ok(title)
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


SOURCES = {"simplify": source_simplify, "ats": source_ats,
           "vansh": source_vansh, "speedy": source_speedy,
           "zshah": source_zshah, "zapply": source_zapply,
           "sndsh": source_sndsh, "jobright": source_jobright,
           "nointernship": source_nointernship}
# Order only breaks ties inside one run; across runs, whichever source sees a
# role first is the one that reports it.
PRIORITY = ["simplify", "ats", "vansh", "speedy", "zshah", "zapply",
            "sndsh", "jobright", "nointernship"]
LABELS = {"simplify": "SimplifyJobs", "ats": "Company portals",
          "vansh": "vanshb03", "speedy": "speedyapply", "zshah": "zshah101",
          "zapply": "zapplyjobs", "sndsh": "sndsh404", "jobright": "Jobright",
          "nointernship": "nointernship.com"}
# derec (DereC4) re-aggregated the sources now read directly, so it only ever
# added lag and duplicates.
RETIRED = {"github", "derec"}                  # old source names to prune from state


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class State:
    """v4 state: cumulative memory with a TTL, replacing v3's per-run
    snapshot.

    v3 stored only the listings that survived *this* run. Anything absent for
    one run — a Workday page shuffle, a listing that a duplicate in another
    source suppressed, or one of Goldman's same-titled roles losing a
    first-come dedupe race — was forgotten, and re-alerted when it came back.
    That produced 1,600 repeat alerts, one listing 73 times.

    Now:
      seen[source][id]  day this source last showed this listing
      roles[key]        day any source last showed this company+role
    A row alerts only if both are unknown. Everything observed refreshes its
    timestamp, kept or suppressed, and entries expire SEEN_TTL_DAYS after
    they were last observed."""

    def __init__(self):
        # Day resolution on purpose: state is committed every run, and a
        # timestamp that changes every run rewrites the whole file each time.
        # Days change once a day, so a run's diff is just its new listings.
        self.now = int(datetime.datetime.now(
            datetime.timezone.utc).timestamp()) // 86400
        self.seen, self.roles, self.migrated = {}, {}, False
        self.boards = {}            # ATS board -> day last observed
        self.held = []              # alerts waiting for quiet hours to end

    @classmethod
    def load(cls):
        st = cls()
        if not os.path.exists(STATE):
            return st
        with open(STATE) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("version") == 4:
            st.seen = {k: dict(v) for k, v in d.get("seen", {}).items()}
            st.roles = dict(d.get("roles", {}))
            st.boards = dict(d.get("boards", {}))
            st.held = [h for h in d.get("held", []) if isinstance(h, dict)]
            return st
        # v1-v3: snapshot sets. Their content is incomplete by construction
        # (flapped listings are missing), so the first v4 run reseeds rather
        # than trusting them; see main().
        st.migrated = True
        src = d.get("sources", {}) if isinstance(d, dict) else {"github": d}
        for k, v in src.items():
            if k not in RETIRED and isinstance(v, list):
                st.seen[k] = {x: st.now for x in v}
        for v in (d.get("dedupe") or {}).values() if isinstance(d, dict) else []:
            for x in v:
                st.roles[x] = st.now
        return st

    def save(self, failed_sources=()):
        cut = self.now - SEEN_TTL_DAYS
        seen = {}
        for src, m in self.seen.items():
            if src in RETIRED:
                continue
            # A source that failed this run was not observed, so its entries
            # must not age out because of the outage.
            seen[src] = m if src in failed_sources else \
                {k: t for k, t in m.items() if t >= cut}
        roles = {k: t for k, t in self.roles.items() if t >= cut}
        boards = {k: t for k, t in self.boards.items() if t >= cut}
        with open(STATE, "w") as f:
            # One key per line keeps git's line diffs small.
            json.dump({"version": 4, "seen": seen, "roles": roles,
                       "boards": boards, "held": self.held},
                      f, indent=0, sort_keys=True)


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
    # JSON publishing keeps everything in the UTF-8 body. The old header-based
    # form crashed on any non-Latin-1 character in a title ("—", "é", "ē"),
    # because HTTP headers are Latin-1 only.
    levels = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}
    payload = {"topic": TOPIC, "title": title, "message": body,
               "priority": levels.get(priority, 4),
               "tags": ["briefcase"], "click": click}
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def entry_lines(r):
    meta = ago(r["age"])
    # Surface a degree ceiling the title does not mention.
    if r["degrees"] and "Bachelor's" not in r["degrees"]:
        meta += (" · " if meta else "") + "/".join(r["degrees"])
    loc = r["loc"] + (f" (+{r['more']} more)" if r.get("more") else "")
    return (f"• {r['company']} — {r['role']}\n   {loc}"
            + (f"   [{meta}]" if meta else "")
            + f"\n   {r['url']}")


def in_quiet_hours(now=None):
    """True when local time is inside QUIET_HOURS ("0-8", or "23-7" which
    wraps past midnight)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", QUIET_HOURS or "")
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    if start == end:
        return False
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(QUIET_TZ)
    except Exception:                               # noqa: BLE001
        tz = datetime.timezone.utc
    now = now or datetime.datetime.now(datetime.timezone.utc)
    h = now.astimezone(tz).hour
    return start <= h < end if start < end else (h >= start or h < end)


_HELD_KEYS = ("company", "role", "loc", "url", "degrees", "more", "sk", "dk")


def hold_record(src, r, today):
    """Row -> what state.json keeps while it waits. The posting date is
    stored rather than its age, so the morning summary shows the true age."""
    h = {k: r.get(k) for k in _HELD_KEYS}
    h.update(src=src, found=today,
             posted=None if r.get("age") is None else today - r["age"])
    return h


def unhold(h, today):
    r = {k: h.get(k) for k in _HELD_KEYS}
    r["degrees"] = r["degrees"] or []
    r["age"] = None if h.get("posted") is None else max(0, today - h["posted"])
    return h.get("src") or "ats", r


def announce(new, link, overnight=False):
    """One push per run across all sources, newest first."""
    new = sorted(new, key=by_recency)
    companies = []
    for r in new:
        if r["company"] not in companies:
            companies.append(r["company"])
    title = ("Overnight: " if overnight else "") + \
        f"{len(new)} new: " + ", ".join(companies[:4])
    if len(companies) > 4:
        title += f" +{len(companies) - 4} more"

    # Fill to the byte budget rather than a fixed count: entries vary from
    # ~90 to ~300 bytes, and ntfy turns anything over 4096 into a file.
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
    if len(body.encode("utf-8")) > NTFY_MAX:
        body = body.encode("utf-8")[:NTFY_MAX - 200].decode("utf-8", "ignore")
        body += f"\n\n…truncated — full list:\n{link}"
    notify(title, body, link)


def _co_key(name):
    """Company name -> comparison key, normalised the same way as canon()."""
    c = _SUFFIX.sub(" ", EMOJI.sub("", name or "").lower())
    return re.sub(r"[^a-z0-9]", "", c)


def _load_tiers():
    """priority.json -> [(tier, alias key)], tier 1 before tier 2."""
    try:
        with open(PRIORITY_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for tier, key in ((1, "tier1"), (2, "tier2")):
        for name, aliases in (d.get(key) or {}).items():
            for a in [name, *aliases]:
                k = _co_key(a)
                if k:
                    out.append((tier, k))
    return out


TIERS = _load_tiers()


def company_tier(name):
    """1 for the MAG-7 + Netflix, 2 for FAANG+, 3 for everyone else. An
    alias matches exactly, or as a prefix when it is 6+ letters, so
    "Amazon Web Services" is Amazon but "Metabase" is not Meta."""
    k = _co_key(name)
    best = 3
    for tier, a in TIERS:
        if tier < best and (k == a or (len(a) >= 6 and k.startswith(a))):
            best = tier
    return best


def by_recency(item):
    """Sort key: newest posting first, unknown age last; within the same
    age, tier 1 then tier 2 companies, then everyone else alphabetically."""
    r = item[1] if isinstance(item, tuple) else item
    return (r["age"] is None, r["age"] or 0, company_tier(r["company"]),
            (r["company"] or "").lower(), (r["role"] or "").lower())


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
    else:
        # One list, newest posting first; unknown dates last; same-age
        # listings grouped by company. Source is a column, not a section.
        rows = [(src, r) for src in PRIORITY for r in new_by_source.get(src) or []]
        rows.sort(key=by_recency)
        out += [f"## New listings ({total}) — newest first", "",
                "| Posted | Company | Role | Location | Source | Link |",
                "|---|---|---|---|---|---|"]
        for src, r in rows:
            role = (r["role"] or "").replace("|", "\\|")
            comp = (r["company"] or "").replace("|", "\\|")
            loc = (r["loc"] or "—").replace("|", "\\|")
            deg = ("<br>" + "/".join(r["degrees"])
                   if r["degrees"] and "Bachelor's" not in r["degrees"] else "")
            more = f" (+{r['more']} more)" if r.get("more") else ""
            out.append(f"| {ago(r['age']) or '—'} | **{comp}** | {role}{deg} | "
                       f"{loc}{more} | {LABELS.get(src, src)} | "
                       f"[apply]({r['url']}) |")
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
    st = State.load()
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

    # Keys that existed before this run started. Comparing against these
    # (not the live maps) is what lets two sources reporting the same new
    # role in one run be merged, rather than the second one looking known.
    prior_roles = set(st.roles)
    prior_index = RoleIndex(prior_roles)     # fuzzy: earlier runs
    run_index = RoleIndex()                  # fuzzy: alerts this run
    new_by_source, by_role, added = {}, {}, []
    for name in active:
        rows = rows_by_source.get(name)
        if rows is None:
            continue
        seeding = name not in st.seen
        mem = st.seen.setdefault(name, {})
        prior_ids = set(mem)
        prior_boards = set(st.boards)
        fresh = dup = board_seeded = 0
        for r in rows:
            unseen = (r["sk"] not in prior_ids and r["dk"] not in prior_roles
                      and not prior_index.match(r["dk"]))
            mem[r["sk"]] = st.now                       # observed: refresh
            st.roles[r["dk"]] = st.now
            # A board just added to boards.json seeds silently, the same way
            # a new source does; otherwise adding a busy board would report
            # its whole current listing as new.
            board = r["sk"].rsplit(":", 1)[0] if name == "ats" else None
            new_board = board is not None and board not in prior_boards
            if board is not None:
                st.boards[board] = st.now
            if seeding or not unseen:
                continue
            if new_board:
                board_seeded += 1
                continue
            # The first v4 run follows a v3 snapshot that is missing every
            # listing that was flapping. Alert only on genuinely fresh
            # postings that run, so the migration can't replay the repeats.
            if st.migrated and (r["age"] is None or r["age"] > 1):
                continue
            if r["dk"] in by_role:                      # same role, this run
                by_role[r["dk"]]["more"] = by_role[r["dk"]].get("more", 0) + 1
                dup += 1
                continue
            if run_index.match(r["dk"]):                # reworded, this run
                dup += 1
                continue
            by_role[r["dk"]] = r
            run_index.add(r["dk"])
            new_by_source.setdefault(name, []).append(r)
            added.append((name, r))
            fresh += 1
        if name == "ats":
            for b in ATS_OK_BOARDS:
                st.boards[b] = st.now
        if seeding:
            print(f"[{name}] seeded baseline with {len(rows)} listings, "
                  "no notification sent")
        else:
            extra = f", {dup} merged as duplicates" if dup else ""
            if board_seeded:
                extra += f", {board_seeded} from newly added boards seeded silently"
            print(f"[{name}] {len(rows)} relevant, {fresh} new{extra}")
    if st.migrated:
        print("[state] migrated to v4 cumulative memory; this run alerted "
              "only on postings up to 1 day old")

    # Everything not yet pushed: listings held from earlier runs (quiet hours,
    # or a failed push) plus this run's. Held rows older than HELD_MAX_DAYS
    # are dropped so a broken topic can't grow the queue forever.
    held = [h for h in st.held
            if st.now - (h.get("found") or st.now) <= HELD_MAX_DAYS]
    pending = held + [hold_record(name, r, st.now) for name, r in added]
    quiet = in_quiet_hours()
    send = [h for h in pending
            if not quiet or company_tier(h.get("company")) <= QUIET_BREAKTHROUGH_TIER]
    st.held = [h for h in pending if h not in send]
    for h in st.held:
        h.setdefault("night", quiet)

    # The digest lists everything pending, so the link opened from a
    # morning summary (or a tier-1 alert at night) shows all of it. Written
    # before notifying so the link the push opens is already correct.
    pending_by_source = {}
    for h in pending:
        src, r = unhold(h, st.now)
        pending_by_source.setdefault(src, []).append(r)
    write_digest(pending_by_source, boards)
    if quiet and st.held:
        print(f"[quiet] holding {len(st.held)} listing(s) until "
              f"quiet hours ({QUIET_HOURS} {QUIET_TZ}) end")
    if send:
        try:
            announce([unhold(h, st.now)[1] for h in send], digest_url(),
                     overnight=not quiet and any(h.get("night") for h in held))
        except Exception as e:                       # noqa: BLE001
            # Keep them queued; the next run sends them.
            print(f"notify FAILED, will retry next run: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            st.held = pending
            failures.append("notify")

    st.save(failed_sources=failures)
    if failures:
        sys.exit(f"failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()