---
name: shared-link-review
description: Use when the owner shares a link or post and asks my take.
version: 1.0.0
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [research, x, twitter, review, ai-agents]
    related_skills: []
---

# Shared link / post review

## When to Use

The owner pastes a URL (X/Twitter, article, GitHub repo) or a screenshot, with or without "what do you think?". Not for sending links to others (see outbound-messages-and-files).

People often drop an X post, article, repo or screenshot with "what do you think?" or no text at all. The deliverable is an opinion grounded in the actual content, not a summary.

## Procedure

1. **Fetch the real content before opining.** Never review from the link preview or title alone.
   - X / Twitter links (`x.com/<user>/status/<id>`): x.com itself won't render. Use the fxtwitter JSON API: `https://api.fxtwitter.com/<user>/status/<id>`. Run `python3 "$HERMES_HOME/skills/research/shared-link-review/scripts/read_x_post.py" '<url>'` via the terminal tool; it prints author, date, text, expanded links, media, and the full article with code blocks.
     - Short posts: `tweet.text`, `tweet.raw_text.facets[].replacement` (expands t.co links, e.g. to a GitHub repo), `tweet.media`, `tweet.quote`.
     - Long-form X Articles (text is just an `x.com/i/article/...` link): body is in `tweet.article.content.blocks[]` (type + text); code samples live in `tweet.article.content.entityMap[]` entries with `data.markdown`.
     - `web_extract` head+tail truncates long article JSON (shows `...` mid-string) and the JSON contains raw control chars, so plain `json.loads` fails. Fetch the full body (the script, or curl to a file in `$TMPDIR`) and parse with `strict=False`.
   - If a post just links elsewhere (repo, paper), follow that link too; it often adds facts (license, platform, what's editable) the post omits.
   - Screenshots: read the image directly; state that you're judging from the screenshot, not the underlying paper.
   - The owner says the post promises a PDF/download they can't find, or wants a copy: follow `references/x-article-pdf.md` (search text + images + profile, then build and verify a PDF with `scripts/x_article_to_html.py`).
2. **Check provenance.** Scan the end of articles for "sponsored", "thanks to X for sponsoring", or an author selling the product. Lead with it if present.
   - Docs or product pages for a platform I run on (for example the iMessage/Photon kit): after fetching the page, check the local install (plugin adapter, sidecar, config) so the review says which listed features already work here and which don't. Don't claim support from the docs alone.
3. **Recognise repeats.** If the owner sends the same link twice in a row, first check the gateway log for whether my earlier reply was actually sent; if it was dropped (connection restart), send a short version of the same take and say it's a resend rather than re-researching.
   Otherwise: If the link is the same research/product as something already reviewed in the conversation, say so in the first line and give only what's new; don't re-review.
4. **Write the reply** (format below).

## Reply format

- First sentence: the verdict, plus any big caveat (sponsored, same as before, only saw a screenshot).
- Explain the idea in plain words with one everyday comparison; define jargon ("harness" = prompts, tools, memory, skills around the model).
- Tie it to the owner's own setup: this agent's tools, who can approve what (only the owner), the hard lines (money, logins, deletion), and the verified-completion rules. Point out gaps the article exposes in the current setup, candidly.
- Weigh evidence: benchmark claims from the authors, LLM-as-judge grading, self-judging without ground truth, and claims I couldn't verify get one short clause each.
- Take a position ("I'd copy the habit, not adopt the tool") and end with one small, concrete next step or offer. No headings or bullets in casual chat; a few short paragraphs.

## Recurring judgments worth reusing

- Self-modifying agents: fine to let them *propose* prompt/skill edits with before/after evidence; keep guardrails and permissions off-limits to self-edits; a human approves each change.
- Every real failure should become a saved regression case (exact input + correct result) rerun before prompt/skill changes ship.
- Approvals should be scoped and expiring (who/what/when/until, anything outside falls back to ask); silence means do nothing.
