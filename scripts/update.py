#!/usr/bin/env python3
"""Monthly updater for the American Capitalism page.

What it does each run (GitHub Actions, 1st of every month):
  1. ARTICLES  - searches Google News RSS and the GDELT news index for every category,
                 keeps the newest reputable articles, and REPLACES a category only if it
                 found at least 3 new ones. Otherwise the current set stays (never blank).
                 No article archive is kept: each month starts fresh.
  2. STATE & LOCAL - rotates through 10 states a month so local Black, Hispanic and
                 minority-business news reaches the page, not just national outlets.
  3. CHART DATA - refreshes figures that have a stable public feed:
                   * Homeownership by race (Census HVS via FRED CSV, no key needed)
                   * Fed Distributional Financial Accounts wealth by race (cross-check)
                   * Census Annual Business Survey employer-firm counts (if a newer year exists)
                 Survey-based series that change methodology (SCF, every 3 years) are NOT
                 overwritten automatically; the script raises an alert on the page instead,
                 so a person checks the new release before the chart changes.
  4. PEOPLE    - collects news mentions of possible new Black founders/inventors into
                 data/people_candidates.json for review. Candidates are never plotted
                 automatically, so nothing unverified reaches the timeline.
Then scripts/build.py embeds the data into index.html.

Nothing here invents data. If a source fails, the old value stays and the failure is logged.
"""
import csv, datetime as dt, io, json, pathlib, re, sys, time, urllib.parse
import xml.etree.ElementTree as ET

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = {"User-Agent": "american-capitalism-ledger/1.0 (+https://github.com)"}
TODAY = dt.date.today()
LOG = []


def log(msg):
    LOG.append(msg)
    print(msg, flush=True)


def load(name):
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def save(name, obj):
    (DATA / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} for {url[:90]}")
        except requests.RequestException as e:
            log(f"  {type(e).__name__} for {url[:90]}")
        time.sleep(2 * (attempt + 1))
    return None


# ---------------------------------------------------------------- articles
# Page order: Black America first, then the diaspora, then comparisons.
CATEGORY_ORDER = ["Black Business & Entrepreneurship", "Racial Wealth Gap", "Black Inventors & Patents", "Black Diaspora",
                  "Capital Access & Investment", "State & Local", "Hispanic & Latino Business", "Research & Projections"]
# Starter articles for a category that has none yet (used only until a scan finds 3+).
SEEDS = {"Black Diaspora": [
    {"title": "10 Caribbean American Women in Business to Support in 2024", "url": "https://www.caribbeannationalweekly.com/posts/10-caribbean-american-women-in-business-to-support-in-2024", "source": "Caribbean National Weekly", "date": "2024-01-12", "summary": "Profiles ten Caribbean American women running businesses across industries."},
    {"title": "Opportunities for Caribbean and Caribbean-American owned businesses", "url": "https://iadb.org/en/news/opportunities-caribbean-and-caribbean-american-owned-businesses", "source": "Inter-American Development Bank", "date": "", "summary": "A Washington briefing connecting Caribbean and Caribbean-American entrepreneurs with local, federal and international buyers."},
    {"title": "12 Caribbean And Caribbean Roots Executives You Should Know", "url": "https://newsamericasnow.com/caribbean-american-heritage-month-12-caribbean-and-caribbean-roots-executives-you-should-know", "source": "News Americas Now", "date": "2017-06-08", "summary": "Corporate leaders born in the Caribbean or with Caribbean family roots."}]}
BLACK_PRESS = ["blackenterprise.com", "essence.com", "theroot.com", "thegrio.com", "blavity.com", "afro.com", "chicagodefender.com",
               "amsterdamnews.com", "jbhe.com", "theemancipator.org", "capitalbnews.org", "word-in-black.com", "newsone.com",
               "caribbeannationalweekly.com", "newsamericasnow.com", "caribbeanlife.com", "sflcn.com", "jamaica-gleaner.com"]

CATEGORY_QUERIES = {
    "Black Diaspora": ['"Caribbean-American" business OR entrepreneur', '"Haitian-American" OR "Jamaican-American" founder business',
                       '"Afro-Latino" OR "Afro-Latina" entrepreneur', '"Black British" OR "Afro-Caribbean" founder United States'],
    "Racial Wealth Gap": ['"racial wealth gap"', '"Black wealth" Federal Reserve', '"Hispanic wealth" OR "Latino wealth" gap'],
    "Black Business & Entrepreneurship": ['"Black-owned businesses"', '"Black entrepreneurs" growth', '"Black-owned" company founded'],
    "Black Inventors & Patents": ['"Black inventor" patent', '"Black inventors"', '"African American inventor" patent'],
    "Hispanic & Latino Business": ['"Latino-owned businesses"', '"Hispanic-owned businesses"', '"Latino entrepreneurs"'],
    "Capital Access & Investment": ['"Black founders" venture capital', '"Latino founders" funding', '"minority depository institutions" OR "Black-owned banks"'],
    "Research & Projections": ['"racial wealth gap" report study', '"wealth gap" projection 2050 Black Hispanic', 'Census "Annual Business Survey" minority-owned'],
}
STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "District of Columbia",
          "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
          "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
          "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
          "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
          "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming"]

# Outlets and publishers weighted up (gov, research, major and Black/Latino press). Others are allowed but rank lower.
TRUSTED = ["census.gov", "federalreserve.gov", "stlouisfed.org", "newyorkfed.org", "atlantafed.org", "frbsf.org", "bls.gov",
           "sba.gov", "mbda.gov", "uspto.gov", "nber.org", "brookings.edu", "pewresearch.org", "urban.org", "epi.org",
           "apnews.com", "reuters.com", "nytimes.com", "washingtonpost.com", "wsj.com", "bloomberg.com", "npr.org", "cnbc.com",
           "forbes.com", "fortune.com", "axios.com", "theatlantic.com", "usatoday.com", "blackenterprise.com", "essence.com",
           "theroot.com", "thegrio.com", "blavity.com", "afro.com", "chicagodefender.com", "amsterdamnews.com", "jbhe.com",
           "theemancipator.org", "capitalbnews.org", "word-in-black.com", "nbcnews.com", "latinousa.org", "nbclatino",
           "aldianews.com", "hispanicexecutive.com", "crunchbase.com", "techcrunch.com", "smithsonianmag.com", "si.edu",
           "bizjournals.com", "governing.com", "stateline.org", "pewtrusts.org"]
BLOCK = ["pinterest.", "facebook.", "x.com", "twitter.", "tiktok.", "instagram.", "reddit.", "youtube.", "msn.com/en-us/feed"]


def google_news(q, days=40):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    r = get(url)
    out = []
    if not r:
        return out
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text if src_el is not None else "") or ""
        site = src_el.get("url", "") if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        pub = it.findtext("pubDate") or ""
        try:
            date = dt.datetime.strptime(pub[:16], "%a, %d %b %Y").date().isoformat()
        except ValueError:
            date = ""
        out.append(dict(title=title, url=it.findtext("link") or "", source=source or site, site=site, date=date, summary=""))
    return out


def gdelt(q):
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
        {"query": f"{q} sourcecountry:US sourcelang:english", "mode": "artlist", "format": "json",
         "maxrecords": 40, "timespan": "1month", "sort": "datedesc"})
    r = get(url)
    if not r:
        return []
    try:
        arts = r.json().get("articles", [])
    except ValueError:
        return []
    out = []
    for a in arts:
        d = a.get("seendate", "")[:8]
        out.append(dict(title=a.get("title", "").strip(), url=a.get("url", ""), source=a.get("domain", ""),
                        site=a.get("domain", ""), date=f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "", summary=""))
    return out


def score(a):
    s = 0
    hay = (a["site"] + " " + a["url"] + " " + a["source"]).lower()
    if any(t in hay for t in TRUSTED):
        s += 10
    if any(t in hay for t in BLACK_PRESS):
        s += 4  # Black and Caribbean press first
    if a["date"]:
        try:
            s += max(0, 5 - (TODAY - dt.date.fromisoformat(a["date"])).days // 7)
        except ValueError:
            pass
    return s


def clean(items, cat):
    seen, keep = set(), []
    for a in sorted(items, key=score, reverse=True):
        if not a["title"] or not re.match(r"^https?://", a["url"] or "") or any(b in a["url"] for b in BLOCK):
            continue  # only plain web links reach the page
        a["title"] = re.sub(r"\s+", " ", a["title"])[:200]
        a["source"] = (a.get("source") or "")[:80]
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        a.pop("site", None)
        a["category"] = cat
        a["collected"] = TODAY.isoformat()
        keep.append(a)
    return keep


def update_articles():
    arts = load("articles")
    old = {}
    for it in arts["items"]:
        old.setdefault(it["category"], []).append(it)
    new_items = []
    queries = dict(CATEGORY_QUERIES)
    month_idx = TODAY.year * 12 + TODAY.month
    states = [STATES[(month_idx * 10 + i) % len(STATES)] for i in range(10)]
    queries["State & Local"] = [f'"Black-owned" OR "Black business" {s}' for s in states] + \
                               [f'"Latino-owned" OR "Hispanic-owned" {s}' for s in states[:5]]
    arts["categories"] = CATEGORY_ORDER
    for cat, seeds in SEEDS.items():
        if not old.get(cat):
            old[cat] = [dict(s, category=cat, collected=TODAY.isoformat()) for s in seeds]
    for cat in arts["categories"]:
        found = []
        for q in queries.get(cat, []):
            found += google_news(q)
            found += gdelt(q)
            time.sleep(1)
        picked = clean(found, cat)[:6]
        if len(picked) >= 3:
            new_items += picked
            log(f"[articles] {cat}: {len(picked)} new")
        else:
            new_items += old.get(cat, [])
            log(f"[articles] {cat}: only {len(picked)} found, kept previous {len(old.get(cat, []))}")
    arts["items"] = new_items
    save("articles", arts)


# ---------------------------------------------------------------- chart data
def fred_last(series_id):
    r = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    if not r:
        return None
    rows = [row for row in csv.reader(io.StringIO(r.text))][1:]
    rows = [(d, v) for d, v in rows if v not in ("", ".")]
    return rows[-1] if rows else None


def quarter(date_str):
    d = dt.date.fromisoformat(date_str)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def update_homeownership(biz):
    ho = biz["homeownershipLatest"]
    vals, periods = {}, set()
    for g, sid in ho["fred"].items():
        last = fred_last(sid)
        if last:
            vals[g] = round(float(last[1]), 1)
            periods.add(quarter(last[0]))
    if len(vals) == 3 and len(periods) == 1:
        ho.update(vals)
        ho["period"] = periods.pop()
        log(f"[homeownership] {ho['period']}: {vals}")
    else:
        log("[homeownership] incomplete feed, kept previous values")


def update_dfa(wealth):
    """Fed Distributional Financial Accounts: share of national wealth by race (quarterly)."""
    r = get("https://www.federalreserve.gov/releases/z1/dataviz/download/dfa-race-levels.csv")
    if not r:
        log("[dfa] download failed, skipped")
        return
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        return
    date_col = next((c for c in rows[0] if c.lower() == "date"), None)
    cat_col = next((c for c in rows[0] if c.lower() == "category"), None)
    nw_col = next((c for c in rows[0] if "net worth" in c.lower()), None)
    if not (date_col and cat_col and nw_col):
        log(f"[dfa] unexpected columns {list(rows[0])[:6]}, skipped")
        return
    latest = max(r_[date_col] for r_ in rows)
    cur = {r_[cat_col].strip().lower(): float(r_[nw_col]) for r_ in rows if r_[date_col] == latest}
    total = sum(cur.values()) or 1
    share = {k: round(100 * v / total, 1) for k, v in cur.items()}
    wealth["dfa"] = {"period": latest, "shareOfWealth": share,
                     "source": {"name": "Federal Reserve Distributional Financial Accounts (wealth by race)",
                                "url": "https://www.federalreserve.gov/releases/z1/dataviz/dfa/distribute/table/"}}
    log(f"[dfa] {latest}: {share}")


def check_new_scf(wealth):
    """SCF is triennial. Raise an alert when a newer survey year appears; a person updates the series."""
    last = wealth["scf"]["series"][-1]["year"]
    nxt = last + 3
    r = get("https://www.federalreserve.gov/econres/scfindex.htm")
    if r and re.search(rf"\b{nxt}\s+Survey of Consumer Finances", r.text):
        return f"Federal Reserve has published the {nxt} Survey of Consumer Finances; update data/wealth.json after review."
    return None


def update_abs(biz):
    """Census Annual Business Survey: try newer data years than we have; update employer-firm counts."""
    have = 2023
    for year in range(TODAY.year, have, -1):
        url = (f"https://api.census.gov/data/{year}/abscs?get=NAME,RACE_GROUP_LABEL,ETH_GROUP_LABEL,FIRMPDEMP,RCPPDEMP"
               f"&for=us:*&NAICS2022=00&SEX=001&VET_GROUP=001")
        r = get(url)
        if not r:
            continue
        try:
            rows = r.json()
        except ValueError:
            continue
        hdr, data = rows[0], rows[1:]
        idx = {h: i for i, h in enumerate(hdr)}
        firms, rec = {}, {}
        for row in data:
            race, eth = row[idx["RACE_GROUP_LABEL"]], row[idx["ETH_GROUP_LABEL"]]
            key = None
            if race == "White" and eth == "Total":
                key = "white"
            elif race == "Black or African American" and eth == "Total":
                key = "black"
            elif race == "Total" and eth == "Hispanic":
                key = "hispanic"
            if key:
                firms[key] = int(row[idx["FIRMPDEMP"]])
                rec[key] = float(row[idx["RCPPDEMP"]]) * 1000  # reported in $1,000s
        if len(firms) == 3:
            data_year = year - 1  # ABS survey year N reports data year N-1
            es = biz["employerShare2023"]
            es["firms"], es["receipts"] = firms, rec
            biz["employerGrowth"]["black"][str(data_year)] = firms["black"]
            biz["employerGrowth"]["hispanic"][str(data_year)] = firms["hispanic"]
            log(f"[abs] data year {data_year}: {firms}")
            return f"Census ABS data year {data_year} loaded automatically; review share percentages in data/business.json."
    log("[abs] no newer data year available")
    return None


# ---------------------------------------------------------------- people candidates
def people_candidates():
    qs = ['"first Black-owned"', '"Black inventor" "patent"', '"Black founder" launches', '"Afro-Latino" founder',
          '"Haitian-American" OR "Jamaican-American" OR "Caribbean-American" founder', '"Black woman" patent invention']
    found = []
    for q in qs:
        found += google_news(q, days=35)
        time.sleep(1)
    out = [{"headline": a["title"], "url": a["url"], "source": a["source"], "date": a["date"],
            "status": "needs review: confirm the person, year and background (where born, where parents came from) from two sources, then add to data/people.json"}
           for a in clean(found, "candidate")[:40]]
    (DATA / "people_candidates.json").write_text(json.dumps({"collected": TODAY.isoformat(), "candidates": out},
                                                            ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[people] {len(out)} candidates queued for review")


def check_resources():
    """Flag any help link that stopped working, so a person can replace it."""
    res = load("resources")
    bad = []
    for g in res.get("groups", []):
        for it in g.get("items", []):
            r = get(it["url"])
            if r is None:
                bad.append(f"Help link may be broken: {it['name']} ({it['url']})")
    log(f"[resources] {len(bad)} link(s) need review")
    return bad


def main():
    alerts = []
    try:
        update_articles()
    except Exception as e:  # never let one step break the page
        log(f"[articles] error {e!r}")
    wealth, biz = load("wealth"), load("business")
    for step in (lambda: update_homeownership(biz), lambda: update_dfa(wealth)):
        try:
            step()
        except Exception as e:
            log(f"[data] error {e!r}")
    for step in (lambda: check_new_scf(wealth), lambda: update_abs(biz)):
        try:
            a = step()
            if a:
                alerts.append(a)
        except Exception as e:
            log(f"[data] error {e!r}")
    save("wealth", wealth)
    save("business", biz)
    try:
        people_candidates()
    except Exception as e:
        log(f"[people] error {e!r}")
    try:
        alerts += check_resources()
    except Exception as e:
        log(f"[resources] error {e!r}")
    meta = load("meta")
    meta.update(lastRun=TODAY.isoformat(), lastUpdated=TODAY.isoformat(), alerts=alerts, log=LOG[-60:])
    save("meta", meta)


if __name__ == "__main__":
    sys.exit(main())
