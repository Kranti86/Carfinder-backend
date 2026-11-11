#!/usr/bin/env python3
"""Programmatic SEO page generator.

One template + one command -> a unique, stats-filled static page per city, plus a
sitemap. Each page's numbers come from the backend /stats endpoint (which is
Redis-cached and refreshed for free by real searches), so generating pages does
NOT burn booking-API credits.

Usage:
    # live (reads real numbers from your deployed backend):
    python generate.py --api-base https://your-backend.herokuapp.com \
                       --site https://book.thecarrentals.globetradee.pro

    # preview with sample numbers, no backend needed:
    python generate.py --demo

Output: ./out/car-rental/<slug>/index.html  and  ./out/sitemap.xml
Schedule it (e.g. Heroku Scheduler, nightly) and push ./out to Fastly.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

CSS = """
:root{--ink:#0d1220;--muted:#5a6274;--line:#e7e9f0;--accent:#4f46e5;--accent-2:#7c6ff0;--soft:#f6f7fb;--accent-soft:#eef2ff;--save:#0a7c42}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:var(--ink);line-height:1.55;letter-spacing:-.011em;background:#fff}
.wrap{max-width:620px;margin:0 auto;padding:0 20px}
.top{padding:16px 0;display:flex;align-items:center;justify-content:space-between}
.mark{font-weight:800;font-size:16px;display:flex;align-items:center;gap:8px}
.mark .b{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--accent),var(--accent-2))}
.mark span{color:var(--muted);font-weight:500}
.crumb{font-size:12px;color:var(--muted);padding:2px 2px 8px}.crumb a{color:var(--muted);text-decoration:none}
.hero{background:linear-gradient(180deg,var(--accent-soft),#fff);border-radius:20px;padding:24px 20px}
h1{font-size:27px;line-height:1.12;letter-spacing:-.02em;font-weight:800}h1 em{font-style:normal;color:var(--accent)}
.lede{margin-top:10px;color:#3a4256;font-size:14.5px}
.statbar{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
.stat{flex:1;min-width:120px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px 13px}
.stat .n{font-size:19px;font-weight:800;color:var(--accent)}.stat .l{font-size:11px;color:var(--muted);margin-top:1px}
.cta{display:block;text-align:center;margin-top:16px;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;text-decoration:none;font-weight:700;font-size:16px;padding:15px;border-radius:13px;box-shadow:0 8px 20px rgba(79,70,229,.25)}
.freshness{font-size:11px;color:var(--muted);text-align:center;margin-top:8px}
section{padding:26px 2px}
h2{font-size:19px;font-weight:800;letter-spacing:-.015em}
p.body{margin-top:10px;color:#3a4256;font-size:14.5px}
.tbl{margin-top:14px;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.tr{display:flex;justify-content:space-between;padding:12px 14px;font-size:14px}.tr+.tr{border-top:1px solid var(--line)}
.tr b{font-weight:700}.tr .p{font-weight:800;color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.chip{font-size:13px;font-weight:600;background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:8px 13px}
a.chip{text-decoration:none;color:inherit;cursor:pointer}a.chip:hover{border-color:var(--accent);color:var(--accent)}
.faq{margin-top:6px}.qa{border-top:1px solid var(--line);padding:14px 2px}
.qa .q{font-weight:700;font-size:14.5px}.qa .a{color:#3a4256;font-size:13.5px;margin-top:5px}
footer{padding:26px 0 40px;color:var(--muted);font-size:12px;text-align:center;border-top:1px solid var(--line);margin-top:20px}
"""

TYPE_ORDER = ["Compact", "Mini", "Mid-size", "Full-size", "Standard", "SUV", "Minivan", "Van", "Estate", "Luxury", "Premium"]


def _order_types(avg: dict) -> list[tuple[str, int]]:
    items = list(avg.items())
    items.sort(key=lambda kv: (TYPE_ORDER.index(kv[0]) if kv[0] in TYPE_ORDER else 99, kv[1]))
    return items


def default_pickups(city: str) -> list[str]:
    return [f"{city} International Airport", f"Downtown {city}", f"{city} city centre"]


def page(city: dict, s: dict, site: str, app_base: str) -> str:
    name = city["name"]
    cur = s.get("currency", "USD")
    sym = "$" if cur == "USD" else ""
    fromday = s.get("cheapest_per_day")
    fromday_txt = f"{sym}{fromday}/day" if fromday else "great rates"
    cars = s.get("car_count") or 0
    sup_n = s.get("supplier_count") or 0
    suppliers = s.get("suppliers") or []
    sup_txt = ", ".join(suppliers[:6]) if suppliers else "top rental companies"
    avg = s.get("avg_by_type") or {}
    types = _order_types(avg)
    pickups = s.get("top_pickups") or default_pickups(name)
    canonical = f"{site}/car-rental/{city['slug']}"
    applink = f"{app_base}/online.html?city={urllib.parse.quote(name)}&lat={city['lat']}&lon={city['lon']}&autosearch=1"
    q = urllib.parse.quote
    updated = date.today().isoformat()

    # unique intro woven from the real numbers
    cheapest_type = types[0][0].lower() if types else "compact"
    suv = avg.get("SUV")
    intro = (
        f"{name} is a busy rental market and prices move with demand. Right now the "
        f"cheapest car in {name} starts at <b>{fromday_txt}</b>, with <b>{cars:,} vehicles</b> "
        f"available across <b>{sup_n} suppliers</b> including {sup_txt}. "
        + (f"SUVs average <b>{sym}{suv}/day</b>. " if suv else "")
        + f"Compare live prices below and reserve online in seconds."
    )

    type_rows = "".join(
        f'<div class="tr"><b>{t}</b> <span class="p">{sym}{p}/day</span></div>' for t, p in types
    ) or '<div class="tr"><b>All types</b> <span class="p">live prices</span></div>'

    # Clickable: each pickup area deep-links into the app pre-filtered to that area.
    pickup_chips = "".join(f'<a class="chip" href="{applink}&area={q(p)}">{p}</a>' for p in pickups[:6])
    # Clickable: each supplier deep-links into the app pre-filtered to that supplier.
    sup_list = suppliers[:10] or ["Avis", "Budget", "Enterprise", "Hertz"]
    supplier_chips = "".join(f'<a class="chip" href="{applink}&supplier={q(x)}">{x}</a>' for x in sup_list)

    faq = [
        (f"How much is a rental car in {name}?",
         f"{name} rentals start at {fromday_txt}" + (f", and SUVs average {sym}{suv}/day" if suv else "") + ", based on live availability updated today."),
        (f"Do I need insurance to rent a car in {name}?",
         "Yes — rental companies hold you liable for damage, so coverage is required. You can add full CDW coverage at checkout, or use your own."),
        (f"Where can I pick up a rental car in {name}?",
         "Most rentals are collected at the airport, with downtown and city-centre depots also available: " + ", ".join(pickups[:4]) + "."),
        (f"Can I book a {name} rental car online instantly?",
         "Yes — compare live prices, pick your car and reserve online in under a minute, no phone call needed."),
    ]
    faq_html = "".join(f'<div class="qa"><div class="q">{q}</div><div class="a">{a}</div></div>' for q, a in faq)
    faq_ld = {"@context": "https://schema.org", "@type": "FAQPage",
              "mainEntity": [{"@type": "Question", "name": q,
                              "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}
    auto_ld = {"@context": "https://schema.org", "@type": "AutoRental",
               "name": f"Therentalcars — {name}", "areaServed": f"{name}, US",
               "url": canonical, "priceRange": "$$", "telephone": "+1-888-527-1977",
               "makesOffer": {"@type": "Offer", "priceCurrency": cur, "price": str(fromday or ""),
                              "description": f"Car rental in {name} from {fromday_txt}"}}

    title = f"Car Rental in {name} from {fromday_txt} — Compare {sup_n} Suppliers | Therentalcars"
    desc = (f"Compare {name} car rentals from {fromday_txt} across {sup_n} suppliers and "
            f"{cars:,} cars. Live prices from {sup_txt}. Reserve online in seconds.")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<style>{CSS}</style>
</head><body>
<div class="wrap">
  <div class="top"><div class="mark"><span class="b"></span>therentalcars<span>.online</span></div></div>
  <nav class="crumb"><a href="{site}/">Home</a> &rsaquo; <a href="{site}/car-rental">Car rental</a> &rsaquo; {name}</nav>
  <div class="hero">
    <h1>Car rental in <em>{name}</em><br>from {fromday_txt}</h1>
    <p class="lede">Compare live prices across <b>{sup_n} suppliers</b> and <b>{cars:,} cars</b> in {name} — reserve online in seconds. No calls.</p>
    <div class="statbar">
      <div class="stat"><div class="n">{fromday_txt}</div><div class="l">Cheapest available</div></div>
      <div class="stat"><div class="n">{cars:,}</div><div class="l">Cars in {name}</div></div>
      <div class="stat"><div class="n">{sup_n}</div><div class="l">Rental suppliers</div></div>
    </div>
    <a class="cta" href="{applink}">See live {name} prices &rarr;</a>
    <div class="freshness">Prices updated {updated} · from live availability</div>
  </div>
  <section><h2>Renting a car in {name}</h2><p class="body">{intro}</p></section>
  <section><h2>Average {name} prices by car type</h2><div class="tbl">{type_rows}</div></section>
  <section><h2>Where to pick up in {name}</h2><div class="chips">{pickup_chips}</div></section>
  <section><h2>Rental companies in {name}</h2><div class="chips">{supplier_chips}</div></section>
  <section><h2>{name} car rental FAQs</h2><div class="faq">{faq_html}</div></section>
  <a class="cta" href="{applink}">Search all {cars:,} {name} cars &rarr;</a>
  <footer>© Therentalcars · {name} car rental · prices from live partner availability, updated daily.</footer>
</div>
<script type="application/ld+json">{json.dumps(auto_ld)}</script>
<script type="application/ld+json">{json.dumps(faq_ld)}</script>
</body></html>"""


def demo_stats(i: int, name: str) -> dict:
    base = 22 + (i % 6) * 2
    return {
        "currency": "USD", "cheapest_per_day": base, "car_count": 260 + i * 37,
        "supplier_count": 4 + i % 4,
        "suppliers": ["Avis", "Budget", "Enterprise", "Hertz", "Dollar", "Thrifty", "Alamo"][: 4 + i % 4],
        "avg_by_type": {"Compact": base, "Mid-size": base + 2, "Full-size": base + 1,
                        "SUV": base + 4, "Minivan": base + 12},
        "top_pickups": [f"{name} International Airport", f"Downtown {name}", f"{name} Beach"],
        "updated_at": "", "days": 3,
    }


def fetch_stats(api_base: str, city: dict) -> dict:
    url = (f"{api_base.rstrip('/')}/stats?lat={city['lat']}&lon={city['lon']}"
           f"&city={urllib.parse.quote(city['name'])}")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="")
    ap.add_argument("--site", default="https://book.thecarrentals.globetradee.pro")
    ap.add_argument("--app-base", default="")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    app_base = args.app_base or args.site

    cities = json.load(open(os.path.join(HERE, "cities.json")))
    urls = []
    for i, city in enumerate(cities):
        try:
            s = demo_stats(i, city["name"]) if args.demo else fetch_stats(args.api_base, city)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {city['slug']}: stats failed ({e}); skipped")
            continue
        html = page(city, s, args.site, app_base)
        d = os.path.join(OUT, "car-rental", city["slug"])
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "index.html"), "w", encoding="utf-8").write(html)
        urls.append(f"{args.site}/car-rental/{city['slug']}")
        print(f"  ✓ {city['slug']}: {s.get('car_count')} cars, from ${s.get('cheapest_per_day')}/day")

    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "".join(f"  <url><loc>{u}</loc><changefreq>daily</changefreq></url>\n" for u in urls)
               + "</urlset>\n")
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "sitemap.xml"), "w", encoding="utf-8").write(sitemap)
    print(f"\nGenerated {len(urls)} pages + sitemap.xml -> {OUT}")


if __name__ == "__main__":
    main()
