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

try:  # turns Google News redirect links into the real article address
    from googlenewsdecoder import gnewsdecoder
except ImportError:  # still runs without it; Google News links are then skipped
    gnewsdecoder = None

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


# ---------------------------------------------------------------- link checks
# A link only reaches the page if it opens as something a browser can show: a web page or a PDF.
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"}
SHOWABLE = ("text/html", "application/xhtml+xml", "application/pdf")
GOOGLE_NEWS = re.compile(r"^https?://news\.google\.com/(rss/)?(articles|read)/")
_checked = {}


def resolve_google(urls):
    """{google_news_url: real_article_url} for the ones that could be decoded."""
    out = {}
    todo = [u for u in dict.fromkeys(urls) if GOOGLE_NEWS.match(u or "")]
    if not todo or gnewsdecoder is None:
        return out
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        try:
            res = gnewsdecoder(chunk, interval=1)
        except Exception as e:
            log(f"  Google News decode error {e!r}")
            continue
        for u, r in zip(chunk, res if isinstance(res, list) else [res]):
            if r and r.get("success") and r.get("decoded_url", "").startswith("http"):
                out[u] = r["decoded_url"]
        time.sleep(1)
    log(f"  decoded {len(out)} of {len(todo)} Google News links")
    return out


def link_status(url):
    """('ok', final_url) · ('bad', reason) · ('unsure', reason).
    'bad' = gone or not something a browser can show (data file, download, error page).
    'unsure' = the site blocks scripts (403/429) or timed out; real visitors usually get through."""
    if url in _checked:
        return _checked[url]
    if not re.match(r"^https?://", url or "") or GOOGLE_NEWS.match(url):
        res = ("bad", "not a direct link")
    else:
        try:
            r = requests.get(url, headers=BROWSER_UA, timeout=20, allow_redirects=True, stream=True)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status_code in (404, 410):
                res = ("bad", f"HTTP {r.status_code}")
            elif r.status_code >= 400:
                res = ("unsure", f"HTTP {r.status_code}")
            elif ctype and not ctype.startswith(SHOWABLE):
                res = ("bad", f"opens as {ctype}")
            else:
                res = ("ok", r.url)
            r.close()
        except requests.Timeout as e:
            res = ("unsure", type(e).__name__)       # slow site: keep, but report
        except requests.ConnectionError as e:
            res = ("bad", "site not reachable")     # no such site, or it refuses connections
        except requests.RequestException as e:
            res = ("unsure", type(e).__name__)
    _checked[url] = res
    return res


def usable_articles(items, limit):
    """Resolve Google News links, drop anything that won't open as a page, keep up to `limit`."""
    real = resolve_google([a["url"] for a in items])
    keep = []
    for a in items:
        url = real.get(a["url"], a["url"])
        state, info = link_status(url)
        if state == "bad":
            continue
        a = dict(a, url=info if state == "ok" else url)
        keep.append(a)
        if len(keep) >= limit:
            break
    return keep


# ---------------------------------------------------------------- articles
# Page order: Black America first, then the diaspora, then comparisons.
CATEGORY_ORDER = ["Black Business & Entrepreneurship", "Racial Wealth Gap", "Black Inventors & Patents", "Black Diaspora",
                  "Capital Access & Investment", "State & Local", "Hispanic & Latino Business", "Research & Projections"]
# Starter articles for a category that has none yet (used only until a scan finds 3+).
# Hand-checked starter articles with DIRECT links, for every category. Used whenever a scan
# finds fewer than 3 working links, so The Wire is never empty and never shows a dead link.
SEEDS = {
 "Racial Wealth Gap": [
  {
   "title": "Black Family Wealth Falls 25% as Racial Wealth Gap Widens, Fed Finds",
   "url": "https://theemancipator.org/2026/10/19/news/black-family-wealth-falls-25-as-racial-wealth-gap-widens-fed-finds/",
   "source": "The Emancipator",
   "date": "2026-10-19",
   "summary": "The Fed’s 2025 survey shows typical Black family wealth dropping to $36,800 while White and Hispanic medians rose."
  },
  {
   "title": "Federal Reserve Board Report Shows Huge Racial Income and Wealth Gaps",
   "url": "https://jbhe.com/2026/11/federal-reserve-board-report-shows-huge-racial-income-and-wealth-gaps/",
   "source": "Journal of Blacks in Higher Education",
   "date": "2026-11-02",
   "summary": "Breaks out 2025 median and mean income and wealth for Black and White families."
  },
  {
   "title": "Black Household Wealth Fell 25%, Per New Fed Data",
   "url": "https://blavity.com/black-household-wealth-fell-25-percent-fed-data",
   "source": "Blavity",
   "date": "2026-10-20",
   "summary": "CEPR analysis of the 2025 SCF, including the widening distance from the national median."
  },
  {
   "title": "Greater Wealth, Greater Uncertainty: Changes in Racial Inequality in the SCF",
   "url": "https://www.federalreserve.gov/econres/notes/feds-notes/greater-wealth-greater-uncertainty-changes-in-racial-inequality-in-the-survey-of-consumer-finances-20231018.html",
   "source": "Federal Reserve Board",
   "date": "2023-10-18",
   "summary": "The Fed’s own 1989–2022 series on wealth and ownership by race and ethnicity."
  }
 ],
 "Black Business & Entrepreneurship": [
  {
   "title": "Census Bureau Releases New Data on Business Owner Characteristics",
   "url": "https://www.census.gov/newsroom/press-releases/2025/business-owner-characteristics.html",
   "source": "U.S. Census Bureau",
   "date": "2025-11-20",
   "summary": "2023 employer-firm counts and receipts by owner race and ethnicity."
  },
  {
   "title": "Black employers are reaching new heights",
   "url": "https://www.brookings.edu/articles/black-employers-are-reaching-new-heights",
   "source": "Brookings Institution",
   "date": "2025",
   "summary": "Black-owned employer firms passed 200,000 for the first time in 2023."
  },
  {
   "title": "A look at Black-owned businesses in the U.S.",
   "url": "https://www.pewresearch.org/short-reads/2025/02/12/a-look-at-black-owned-businesses-in-the-us/",
   "source": "Pew Research Center",
   "date": "2025-02-12",
   "summary": "Counts, revenue, sectors and states for Black-owned employer firms."
  },
  {
   "title": "Black Americans Own Three Percent of Employer Businesses",
   "url": "https://jbhe.com/2025/01/black-americans-own-three-percent-of-employer-business-in-the-united-states",
   "source": "Journal of Blacks in Higher Education",
   "date": "2025-01",
   "summary": "Compares Black business ownership with population share."
  }
 ],
 "Black Inventors & Patents": [
  {
   "title": "Beyond Baker’s list: Black innovation then and now",
   "url": "https://www.uspto.gov/blog/beyond-baker-rsquo-s-list",
   "source": "U.S. Patent and Trademark Office",
   "date": "",
   "summary": "How Henry Baker’s lists documented early Black patent holders, with modern inventors."
  },
  {
   "title": "Black Inventors and Innovators report",
   "url": "https://webcf.waybackmachine.org/web/20230329153711/https://invention.si.edu/sites/default/files/Lemelson-Center-Black-Inventors-Innovators-Report-updated-2021-10.pdf",
   "source": "Smithsonian Lemelson Center",
   "date": "2021-10",
   "summary": "History of Black patenting, the barriers inventors faced, and counts from Baker to Sluby."
  },
  {
   "title": "Celebrating Black Inventors on World IP Day",
   "url": "https://www.fr.com/wp-content/uploads/2021/04/04-23-21-NLJ-Celebrating-Black-Inventors-on-World-IP-Day-Davis.pdf",
   "source": "National Law Journal / Fish & Richardson",
   "date": "2021-04-23",
   "summary": "Patent attorneys trace landmark Black inventions and the legal hurdles behind them."
  },
  {
   "title": "African American Technological Contributions: Past, Present and Future",
   "url": "https://www.nist.gov/speech-testimony/african-american-technological-contributions-past-present-and-future",
   "source": "NIST",
   "date": "",
   "summary": "Federal testimony on Black contributions to American technology."
  }
 ],
 "Hispanic & Latino Business": [
  {
   "title": "Hispanic-Owned Businesses",
   "url": "https://census.gov/library/stories/2024/10/hispanic-owned-businesses.html",
   "source": "U.S. Census Bureau",
   "date": "2024-10",
   "summary": "406,086 Hispanic-owned employer firms in 2021, led by construction and food service."
  },
  {
   "title": "Hispanic-Owned Businesses on the Upswing",
   "url": "https://www.census.gov/newsroom/blogs/random-samplings/2016/12/hispanic-owned_busin.html",
   "source": "U.S. Census Bureau",
   "date": "2016-12",
   "summary": "Growth of Hispanic-owned firms in the last all-firms survey."
  },
  {
   "title": "2024 Diverse Founder Report",
   "url": "https://harlem.capital/wp-content/uploads/2025/03/2024-HCP-Diverse-Founder-Report.pdf",
   "source": "Harlem Capital",
   "date": "2025-03",
   "summary": "Venture dollars raised by Latino-led and Black-led startups, 2012–2024."
  }
 ],
 "Capital Access & Investment": [
  {
   "title": "Black founders received just 0.4% of all startup funding in 2024",
   "url": "https://peopleofcolorintech.com/articles/black-founders-received-just-0-4-of-all-startup-funding-in-2024/",
   "source": "POCIT (citing Crunchbase)",
   "date": "2025-03-17",
   "summary": "Black founders raised about $730 million of U.S. venture funding in 2024."
  },
  {
   "title": "Startup funding share for Black founders down",
   "url": "https://news.crunchbase.com/diversity/startup-funding-share-black-founders-down-cleantech-healthcare/",
   "source": "Crunchbase News",
   "date": "",
   "summary": "Sector-by-sector look at the shrinking share of venture dollars to Black founders."
  },
  {
   "title": "Latino founders accounted for 2% of all venture funding last year",
   "url": "https://dot.la/tag/vcfamilia",
   "source": "dot.LA",
   "date": "2022-01-26",
   "summary": "Latino investors respond to 2021 data on venture funding."
  }
 ],
 "State & Local": [
  {
   "title": "African Americans in Business",
   "url": "https://indyencyclopedia.org/african-americans-in-business/",
   "source": "Encyclopedia of Indianapolis",
   "date": "",
   "summary": "Indianapolis Black business history from 1880s carriage makers to Mays Chemical."
  },
  {
   "title": "Warren Coleman (1849–1904), L-91",
   "url": "https://www.dncr.nc.gov/blog/2024/01/16/warren-coleman-1849-1904-l-91",
   "source": "N.C. Dept. of Natural and Cultural Resources",
   "date": "2024-01-16",
   "summary": "The first Black-owned and -operated textile mill in the nation, in Concord, N.C."
  },
  {
   "title": "Foodways to Freedom: African Heritage Entrepreneurs in Rhode Island",
   "url": "https://www.rihs.org/?p=9240",
   "source": "Rhode Island Historical Society",
   "date": "",
   "summary": "Caterers, cheesemakers and restaurateurs of 18th- and 19th-century Rhode Island."
  }
 ],
 "Research & Projections": [
  {
   "title": "Wealth of Two Nations: The U.S. Racial Wealth Gap, 1860–2020",
   "url": "https://www.nber.org/system/files/working_papers/w30101/w30101.pdf",
   "source": "NBER",
   "date": "2022",
   "summary": "The longest-run estimate of the Black–White wealth gap, with projections."
  },
  {
   "title": "The Road to Zero Wealth",
   "url": "https://www.coloradotrust.org/wp-content/uploads/sites/2/2017/11/road_to_zero_wealth.pdf",
   "source": "Institute for Policy Studies & Prosperity Now",
   "date": "2017-09",
   "summary": "Trend projections for Black and Latino median wealth."
  },
  {
   "title": "U.S. Wealth Inequality: Gaps Remain Despite Widespread Wealth Gains",
   "url": "https://www.stlouisfed.org/open-vault/2024/feb/us-wealth-inequality-widespread-gains-gaps-remain",
   "source": "St. Louis Fed",
   "date": "2024-02",
   "summary": "How 2019–2022 gains were shared across groups."
  },
  {
   "title": "Racial and Ethnic Inequalities in Household Wealth Persist",
   "url": "https://libertystreeteconomics.newyorkfed.org/2024/06/racial-and-ethnic-inequalities-in-household-wealth-persist",
   "source": "New York Fed, Liberty Street Economics",
   "date": "2024-06",
   "summary": "Tracks group wealth with the Fed’s quarterly distributional accounts."
  }
 ],
 "Black Diaspora": [
  {
   "title": "10 Caribbean American Women in Business to Support in 2024",
   "url": "https://www.caribbeannationalweekly.com/posts/10-caribbean-american-women-in-business-to-support-in-2024",
   "source": "Caribbean National Weekly",
   "date": "2024-01-12",
   "summary": "Profiles ten Caribbean American women running businesses across industries."
  },
  {
   "title": "Opportunities for Caribbean and Caribbean-American owned businesses",
   "url": "https://iadb.org/en/news/opportunities-caribbean-and-caribbean-american-owned-businesses",
   "source": "Inter-American Development Bank",
   "date": "",
   "summary": "A Washington briefing connecting Caribbean and Caribbean-American entrepreneurs with local, federal and international buyers."
  },
  {
   "title": "12 Caribbean And Caribbean Roots Executives You Should Know",
   "url": "https://newsamericasnow.com/caribbean-american-heritage-month-12-caribbean-and-caribbean-roots-executives-you-should-know",
   "source": "News Americas Now",
   "date": "2017-06-08",
   "summary": "Corporate leaders born in the Caribbean or with Caribbean family roots."
  }
 ]
}
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
    # previous articles must still open as real pages; any old Google News redirect is resolved or dropped
    for cat in list(old):
        old[cat] = usable_articles(old[cat], 6)
    for cat, seeds in SEEDS.items():
        if len(old.get(cat, [])) < 3:
            old[cat] = usable_articles(old.get(cat, []) + [dict(s, category=cat, collected=TODAY.isoformat()) for s in seeds], 6)
    for cat in arts["categories"]:
        found = []
        for q in queries.get(cat, []):
            found += google_news(q)
            found += gdelt(q)
            time.sleep(1)
        picked = usable_articles(clean(found, cat)[:18], 6)
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
            state, info = link_status(it["url"])
            if state == "bad":
                bad.append(f"Help link may be broken: {it['name']} ({it['url']}: {info})")
    log(f"[resources] {len(bad)} link(s) need review")
    return bad


def check_source_links():
    """Every other link on the page (sources, projections, people). Problems go to the Actions log and to
    data/meta.json -> linkReport, so a person can replace them; nothing is removed automatically."""
    urls = {}
    def walk(o, where):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{where}.{k}")
        elif isinstance(o, list):
            for v in o:
                walk(v, where)
        elif isinstance(o, str) and re.match(r"^https?://", o):
            urls.setdefault(o, where)
    for part in ("people", "wealth", "business"):
        walk(load(part), part)
    report = []
    for u, where in urls.items():
        state, info = link_status(u)
        if state != "ok":
            report.append({"url": u, "where": where, "problem": info, "status": state})
            log(f"  [{state}] {u[:100]} ({info}) in {where}")
    log(f"[links] checked {len(urls)} source links: {sum(r['status']=='bad' for r in report)} broken, "
        f"{sum(r['status']=='unsure' for r in report)} could not be confirmed")
    return report


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
    link_report = []
    try:
        link_report = check_source_links()
    except Exception as e:
        log(f"[links] error {e!r}")
    meta = load("meta")
    meta.update(lastRun=TODAY.isoformat(), lastUpdated=TODAY.isoformat(), alerts=alerts, log=LOG[-60:],
                linkReport=link_report)
    save("meta", meta)


if __name__ == "__main__":
    sys.exit(main())
