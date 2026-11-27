#!/usr/bin/env python3
"""Build a printable HTML copy of an X long-form Article (text, code blocks, all images).

Usage: python3 x_article_to_html.py <x.com status URL> <out.html>
Then print it to PDF in the browser (see references/x-article-pdf.md).
"""
import html, json, re, sys, urllib.request

url, out_path = sys.argv[1], sys.argv[2]
m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", url) or re.search(r"([^/]+)/status/(\d+)", url)
if not m:
    sys.exit("could not parse user/status id from: " + url)
user, sid = m.groups()
raw = urllib.request.urlopen(urllib.request.Request(
    f"https://api.fxtwitter.com/{user}/status/{sid}", headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode()
t = json.loads(raw, strict=False)["tweet"]
a = t.get("article") or sys.exit("not an X Article (no tweet.article)")
c = a["content"]
em = c.get("entityMap")
ents = {str(e["key"]): e["value"] for e in em} if isinstance(em, list) else (em or {})
# MEDIA entities reference mediaItems[].mediaId; the image URL lives in article.media_entities
murl = {}
for me in a.get("media_entities") or []:
    mid = me.get("media_id") or me.get("id")
    u = (me.get("media_info") or {}).get("original_img_url")
    if mid and u:
        murl[str(mid)] = u

out, lst, missing = [], None, 0
cover = ((a.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
if cover:
    out.append(f"<img src='{cover}'>")
out.append(f"<h1>{html.escape(a.get('title',''))}</h1><p class='by'>By @{user} \u00b7 {t.get('created_at','')} \u00b7 {t.get('url','')}</p>")
for b in c.get("blocks", []):
    ty = b["type"]; tx = html.escape(b["text"]).replace("\n", "<br>")
    li = ty in ("unordered-list-item", "ordered-list-item")
    tag = "ul" if ty == "unordered-list-item" else "ol"
    if lst and (not li or lst != tag):
        out.append(f"</{lst}>"); lst = None
    if li:
        if not lst:
            out.append(f"<{tag}>"); lst = tag
        out.append(f"<li>{tx}</li>"); continue
    if ty == "atomic":
        for r in b.get("entityRanges", []):
            d = (ents.get(str(r["key"])) or {}).get("data", {})
            if d.get("markdown"):
                code = d["markdown"].strip().strip("`")
                code = code.split("\n", 1)[1] if "\n" in code else code
                out.append(f"<pre>{html.escape(code)}</pre>")
            for mi in d.get("mediaItems") or []:
                u = murl.get(str(mi.get("mediaId")))
                if u:
                    out.append(f"<img src='{u}'>")
                else:
                    missing += 1
            if d.get("caption"):
                out.append(f"<p class='cap'>{html.escape(d['caption'])}</p>")
        continue
    if not tx.strip():
        continue
    out.append({"header-two": f"<h2>{tx}</h2>", "blockquote": f"<blockquote>{tx}</blockquote>"}.get(ty, f"<p>{tx}</p>"))
if lst:
    out.append(f"</{lst}>")
css = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:720px;margin:auto;font-size:11.5pt;line-height:1.45;color:#111}"
       "h1{font-size:22pt}h2{margin-top:24px;font-size:15pt}img{width:100%;margin:10px 0;page-break-inside:avoid}"
       "pre{background:#f4f4f4;padding:10px;font-size:9pt;white-space:pre-wrap;page-break-inside:avoid}"
       "blockquote{font-style:italic;border-left:3px solid #ccc;padding-left:10px}.by,.cap{color:#666;font-size:9.5pt}")
open(out_path, "w").write(f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{''.join(out)}</body></html>")
print(f"wrote {out_path}: images={len(murl) + (1 if cover else 0)} missing_images={missing}")
