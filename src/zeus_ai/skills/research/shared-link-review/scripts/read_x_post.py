#!/usr/bin/env python3
"""Print an X/Twitter post (incl. long-form Articles) via the fxtwitter API.

Usage: python3 read_x_post.py <x.com status URL or user/status/id>
"""
import json, re, sys, urllib.request

url = sys.argv[1]
m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", url) or re.search(r"([^/]+)/status/(\d+)", url)
if not m:
    sys.exit("could not parse user/status id from: " + url)
user, sid = m.groups()
api = f"https://api.fxtwitter.com/{user}/status/{sid}"
raw = urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode()
t = json.loads(raw, strict=False)["tweet"]  # strict=False: article text has raw control chars

print("AUTHOR:", t["author"].get("name"), "@" + t["author"].get("screen_name", ""))
print("DATE:", t.get("created_at"))
print("TEXT:", t.get("text"))
for f in (t.get("raw_text") or {}).get("facets", []):
    if f.get("replacement"):
        print("LINK:", f["replacement"])
for p in (t.get("media") or {}).get("all", []):
    print("MEDIA:", p.get("type"), p.get("url"))
if t.get("quote"):
    print("QUOTED:", t["quote"].get("text"))

a = t.get("article") or {}
if a:
    print("\nARTICLE:", a.get("title"))
    cover = ((a.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
    if cover:
        print("COVER IMAGE:", cover)
    for me in a.get("media_entities") or []:
        u = (me.get("media_info") or {}).get("original_img_url")
        if u:
            print("ARTICLE IMAGE:", u)  # graphics may hold claims/offers not in the text
    content = a.get("content") or {}
    em = content.get("entityMap")
    ents = {e["key"]: e["value"] for e in em} if isinstance(em, list) else (em or {})
    for b in content.get("blocks", []):
        if b["text"].strip():
            print(f"[{b['type']}] {b['text']}")
        elif b["type"] == "atomic":
            for r in b.get("entityRanges", []):
                md = (ents.get(str(r["key"])) or {}).get("data", {}).get("markdown")
                if md:
                    print("CODE:\n" + md)
