"""Public SEO location pages, served on the main site through the frontend proxy:

  /car-rental                 all published places
  /car-rental/{slug}          one place: real prices, car types, suppliers, FAQ
  /car-rental/sitemap.xml     for Google
  /car-rental/popular.json    top places, for the "Popular locations" block on the home page

Pages are only indexable when reached through the main domain (the frontend proxy
adds X-TRC-Site: main). Direct hits on the Railway backend domain get noindex, so
Google never sees two copies.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from ..services import seo

router = APIRouter(prefix="/car-rental", tags=["seo"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates_seo"))

PHONE = "+1 (888) 527-1977"
PHONE_TEL = "+18885271977"


def site_base() -> str:
    return (os.environ.get("SITE_BASE_URL") or "https://online.thecarrentals.globetradee.pro").rstrip("/")


def _indexable(request: Request) -> bool:
    return request.headers.get("x-trc-site", "") == "main"


def _headers(request: Request) -> dict:
    h = {"Cache-Control": "public, max-age=1800"}
    if not _indexable(request):
        h["X-Robots-Tag"] = "noindex"
    return h


def _aware(d):
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def ago(d: datetime | None) -> str:
    d = _aware(d)
    if not d:
        return "recently"
    days = (datetime.now(timezone.utc) - d).days
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


def short_name(row) -> str:
    return row.name


def search_link(row) -> str:
    q = {"city": row.name, "lat": f"{row.lat:.4f}", "lon": f"{row.lon:.4f}", "autosearch": "1",
         "utm_source": "seo", "utm_campaign": row.slug}
    if row.iata:
        q["iata"] = row.iata
    return f"{site_base()}/online?{urlencode(q)}"


def type_rows(st: dict) -> list[dict]:
    mins, avgs = st.get("min_by_type") or {}, st.get("avg_by_type") or {}
    rows = [{"type": t, "from": mins.get(t), "typical": avgs.get(t)}
            for t in sorted(set(mins) | set(avgs))]
    rows.sort(key=lambda r: (r["from"] if r["from"] is not None else r["typical"] or 1e9))
    return rows


def trend(hist) -> list[dict]:
    """Weekly lows from the daily history (oldest first)."""
    weeks: dict[str, float] = {}
    for h in hist:
        if h.cheapest_per_day is None:
            continue
        d = datetime.strptime(h.day, "%Y-%m-%d")
        key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        weeks[key] = min(weeks.get(key, 1e9), h.cheapest_per_day)
    out = []
    for k in sorted(weeks)[-8:]:
        y, w = k.split("-W")
        start = datetime.fromisocalendar(int(y), int(w), 1)
        out.append({"label": f"Week of {start:%b %d}", "low": round(weeks[k])})
    return out


def faqs(row, st: dict, rows: list[dict]) -> list[tuple[str, str]]:
    where = row.name
    cheapest = st.get("cheapest_per_day")
    out = []
    if cheapest is not None:
        first = rows[0]["type"] if rows else "economy"
        out.append((f"How much does it cost to rent a car at {where}?",
                    f"Recent prices start from about ${cheapest:,.0f} per day, with "
                    f"{first.lower()} cars usually the cheapest. Prices change with dates and "
                    f"demand, so check today's price before you book."))
    suv = next((r for r in rows if "suv" in r["type"].lower()), None)
    if suv and suv["typical"]:
        out.append((f"How much is an SUV rental at {where}?",
                    f"SUVs typically cost around ${suv['typical']:,.0f} per day here, "
                    f"from about ${suv['from'] or suv['typical']:,.0f} per day."))
    if st.get("supplier_count"):
        names = ", ".join((st.get("suppliers") or [])[:6])
        out.append((f"Which rental companies operate at {where}?",
                    f"We found {st['supplier_count']} rental companies here recently"
                    + (f", including {names}." if names else ".")))
    out.append(("What do I pay now and what at the counter?",
                "You pay a small booking fee online when you reserve. The rental itself is "
                "paid to the rental company when you pick up the car."))
    out.append(("Can I cancel my booking?",
                "Many cars come with free cancellation; it's shown on each car before you book. "
                f"For help, call us 24/7 on {PHONE}."))
    return out


def _jsonld(row, faq_list, url) -> str:
    data = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": site_base() + "/"},
            {"@type": "ListItem", "position": 2, "name": "Car rental locations",
             "item": site_base() + "/car-rental"},
            {"@type": "ListItem", "position": 3, "name": row.name, "item": url}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq_list]},
    ]
    return json.dumps(data).replace("</", "<\\/")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    rows = await seo.published()
    items = []
    for r in rows:
        st = seo.stats_of(r)
        items.append({"slug": r.slug, "name": r.name, "kind": r.kind, "region": r.region,
                      "from": st.get("cheapest_per_day"), "cars": st.get("car_count")})
    airports = sorted([i for i in items if i["kind"] == "airport"], key=lambda i: i["name"])
    cities = sorted([i for i in items if i["kind"] != "airport"], key=lambda i: i["name"])
    return templates.TemplateResponse(request=request, name="index.html", headers=_headers(request),
                                      context={"airports": airports, "cities": cities,
                                               "base": site_base(), "phone": PHONE,
                                               "phone_tel": PHONE_TEL,
                                               "indexable": _indexable(request)})


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    rows = await seo.published()
    urls = [f"<url><loc>{site_base()}/car-rental</loc><changefreq>daily</changefreq></url>"]
    for r in rows:
        lastmod = (_aware(r.updated_at) or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        urls.append(f"<url><loc>{site_base()}/car-rental/{escape(r.slug)}</loc>"
                    f"<lastmod>{lastmod}</lastmod><changefreq>daily</changefreq></url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(urls) + "</urlset>")
    return Response(xml, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/popular.json", include_in_schema=False)
async def popular(limit: int = 12):
    rows = await seo.published(limit=max(1, min(limit, 30)))
    data = []
    for r in rows:
        st = seo.stats_of(r)
        data.append({"name": r.name, "url": f"/car-rental/{r.slug}",
                     "from_per_day": st.get("cheapest_per_day"), "kind": r.kind})
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=1800",
                                       "Access-Control-Allow-Origin": "*"})


@router.get("/{slug}", response_class=HTMLResponse)
async def place(request: Request, slug: str):
    row, hist = await seo.get_place(slug)
    if not row or not seo.is_published(row):
        return HTMLResponse("<h1>Not found</h1><p><a href='/car-rental'>All locations</a></p>",
                            status_code=404, headers={"X-Robots-Tag": "noindex"})
    st = seo.stats_of(row)
    rows = type_rows(st)
    faq_list = faqs(row, st, rows)
    url = f"{site_base()}/car-rental/{row.slug}"
    nearby = []
    for other in await seo.published():
        if other.slug != row.slug:
            d = seo.km(row.lat, row.lon, other.lat, other.lon)
            if d <= 250:
                nearby.append((d, other))
    nearby = [{"name": o.name, "slug": o.slug,
               "from": seo.stats_of(o).get("cheapest_per_day")} for _, o in sorted(nearby, key=lambda t: t[0])[:6]]
    cheapest = st.get("cheapest_per_day")
    title = (f"Car Rental at {row.name}" + (f" from ${cheapest:,.0f}/day" if cheapest else "")
             + " | Therentalcars")
    desc = (f"Compare {st.get('car_count') or 'many'} rental cars at {row.name}"
            + (f" from ${cheapest:,.0f}/day" if cheapest else "")
            + f" across {st.get('supplier_count') or 'top'} companies. Prices checked {ago(row.updated_at)}. "
            "Reserve online, pay the rental at pick-up.")
    return templates.TemplateResponse(request=request, name="place.html", headers=_headers(request),
                                      context={
        "r": row, "st": st, "rows": rows, "faqs": faq_list, "trend": trend(hist),
        "updated": ago(row.updated_at), "search_url": search_link(row), "url": url,
        "title": title, "desc": desc, "jsonld": _jsonld(row, faq_list, url), "nearby": nearby,
        "base": site_base(), "phone": PHONE, "phone_tel": PHONE_TEL,
        "indexable": _indexable(request)})
