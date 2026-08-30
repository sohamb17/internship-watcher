#!/usr/bin/env python3
"""
Watch SimplifyJobs/Summer2027-Internships for new listings and push to ntfy.

The README uses HTML <table> blocks, not markdown pipe tables, so a line diff
does not work. This parses rows properly, keys on (company, role) so that edits
to existing rows are ignored, and only reports genuinely new entries.

Env:
  NTFY_TOPIC   required
  STATE_FILE   default state.json
"""
import html
import json
import os
import re
import sys
import urllib.request

SRC = ("https://raw.githubusercontent.com/SimplifyJobs/"
       "Summer2027-Internships/dev/README.md")
STATE = os.environ.get("STATE_FILE", "state.json")
TOPIC = os.environ.get("NTFY_TOPIC")

# Sections you care about. Drop entries to widen or narrow.
SECTIONS = {"Software Engineering", "Data Science, AI & Machine Learning"}

# 🛂 no sponsorship · 🇺🇸 citizenship required · 🔒 closed
EXCLUDE_FLAGS = "🛂🇺🇸🔒"

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u20E3]")


def clean(s):
    s = re.sub(r"<br\s*/?>", " / ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def parse(txt):
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


def key(r):
    return f"{r['company']}|{r['role']}"


def notify(title, body, priority="high"):
    if not TOPIC:
        print("NTFY_TOPIC unset, printing instead:\n", title, "\n", body)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "briefcase",
            "Click": "https://github.com/SimplifyJobs/Summer2027-Internships",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()


def main():
    txt = urllib.request.urlopen(SRC, timeout=60).read().decode("utf-8")
    rows = parse(txt)
    if len(rows) < 100:
        sys.exit(f"only parsed {len(rows)} rows - format probably changed, refusing to run")

    keep = [r for r in rows
            if r["section"] in SECTIONS
            and not any(f in r["flags"] for f in EXCLUDE_FLAGS)]

    if os.path.exists(STATE):
        seen = set(json.load(open(STATE)))
    else:
        seen = None  # first run

    current = {key(r) for r in keep}
    json.dump(sorted(current), open(STATE, "w"), indent=0)

    if seen is None:
        print(f"seeded baseline with {len(current)} listings, no notification sent")
        return

    new = [r for r in keep if key(r) not in seen]
    print(f"parsed {len(rows)} rows, {len(keep)} relevant, {len(new)} new")
    if not new:
        return

    # FAANG+ first, then advanced-degree, then the rest
    new.sort(key=lambda r: (("🔥" not in r["flags"]), ("🎓" not in r["flags"])))

    companies = []
    for r in new:
        if r["company"] not in companies:
            companies.append(r["company"])
    title = "New: " + ", ".join(companies[:4])
    if len(companies) > 4:
        title += f" +{len(companies) - 4} more"

    lines = []
    for r in new[:25]:
        tag = "🔥" if "🔥" in r["flags"] else ("🎓" if "🎓" in r["flags"] else "•")
        lines.append(f"{tag} {r['company']} — {r['role']}\n   {r['loc']}\n   {r['url']}")
    if len(new) > 25:
        lines.append(f"\n...and {len(new) - 25} more")

    notify(title, "\n\n".join(lines))


if __name__ == "__main__":
    main()
