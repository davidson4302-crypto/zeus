# Finding a promised download / making a PDF of an X Article

Use when the owner says a post promises a PDF, guide, or template they can't find, or asks for a copy of an article.

## 1. Look for the real file first
- Grep the full fxtwitter JSON (not the truncated web_extract view) for `pdf`, `page`, `download`, `gumroad`, `link in`, `comment`, and list every `https://pbs.twimg.com/media/...` URL.
- Check the images too: the offer is often only in a graphic. Fast way: open a `data:text/html` page in the browser with all images in a grid (`?name=small`), then `print(capture_screenshot())` and read it. (capture_screenshot's path only comes back if printed.)
- Check the author profile (`https://api.fxtwitter.com/<user>`) for a website/newsletter link.
- "Comment X and I'll DM the PDF" offers live in replies, and x.com won't show replies logged out. If text, images and profile have no link, say plainly you couldn't find it and that such files usually arrive by DM; don't invent a link.

## 2. Fallback: build the PDF yourself (local, reversible; just do it)
1. `python3 scripts/x_article_to_html.py '<url>' "$TMPDIR/article.html"` (expect `missing_images=0`).
2. In browser_exec: read the file, `goto_url("data:text/html;base64," + b64)`, poll until `[...document.images].every(i=>i.complete && i.naturalWidth>0)`, then
   `cdp("Page.printToPDF", printBackground=True, paperWidth=8.5, paperHeight=11, marginTop=0.6, marginBottom=0.6, marginLeft=0.6, marginRight=0.6)` and base64-decode `data` into `$HERMES_HOME/workspace/<Title>.pdf`.
3. Read it back before reporting: page count via
   `osascript -l JavaScript -e 'ObjC.import("PDFKit"); $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath("<abs path>")).pageCount'`
   (`mdls kMDItemNumberOfPages` is null for fresh files; grepping `/Count` is unreliable), and `read_file` on the PDF to confirm title and text are present.
4. Attach with `MEDIA:/abs/path.pdf` and say how it differs from what was promised (e.g. page count, since the graphics make it longer).
