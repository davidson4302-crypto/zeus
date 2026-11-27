"""stockfallback: what the chat agent does when an item is out of stock at one of the owner's stores.

Nothing else the agent is given (its SOUL, the purchase lock, which only refuses a press without the owner's yes, this
plugin's reference, the owner's standing rules) says what to do about an out-of-stock item, so without this the model
guesses: it stops at "it's out" with a question, or reaches for another product.

What this adds, only with the switch file `stock-fallback-on` in the life folder: ONE paragraph in the turn's private
reference, and only on a turn that is about buying (the owner's message names one of their stores or a buying word, or
one of the last few messages names a store or a cart; code decides, nothing is read from disk). The paragraph: look for
the SAME item at the owner's other stores, get it ready there, and ask once with the store and the full total. It grants
nothing: a yes for one store never covers another, nothing is bought without the owner's yes, and the purchase lock's
own code is not touched and still has the last word (no grant for that store and total, no Place order).

The stores come from config: `stores.shopping` (the stores things are bought at) and `stores.flights` (the one store
flights are booked at). Standard library only. Loaded by the plugin only when the switch file is there.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


def _settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


settings = _settings()

SHOPPING = [str(x) for x in (settings.get('stores.shopping', ['Walmart', "Sam's Club", 'Amazon']) or []) if str(x).strip()]
FLIGHTS = str(settings.get('stores.flights', 'Southwest') or '').strip()


def _store_rx(name: str) -> str:
    """A store name as a forgiving regex: optional apostrophe, any spacing ("Sam's Club", "sams club", "sam's")."""
    words = re.sub(r"[’']", '', name.lower()).split()
    if not words:
        return ''
    first = re.escape(words[0][:-1]) + r"'?" + re.escape(words[0][-1]) if words[0].endswith('s') and len(words[0]) > 1 else re.escape(words[0])
    rest = ''.join(r'\s*' + re.escape(w) for w in words[1:])
    return first + (f'(?:{rest})?' if rest and words[0].endswith('s') else rest)


SWITCH = 'stock-fallback-on'
LOOK_BACK = 6            # messages of the conversation that are looked at, the owner's and the agent's
STORES = '|'.join(r for r in (_store_rx(n) for n in SHOPPING) if r) or r'(?!x)x'
# the owner's own message: a store, or a buying word ("in order to" is not one)
BUYING = re.compile(r"\b(?:" + STORES + r"|re-?order(?:ed|ing|s)?|(?<!in )order(?:ed|ing|s)?|buy(?:ing|s)?|bought|purchas(?:e|ed|ing)"
                    r"|cart|check-?out|refills?|restock(?:ed|ing)?|groceries|grocery|out of stock|sold out|in stock|back-?order(?:ed)?)\b", re.I)
# the last few messages: only a store or a word that is about a cart, so the agent's own "in the requested order" or "no
# credit purchase is needed" does not make a turn about buying (the wide buying words fire on many unrelated turns)
RECENT = re.compile(r"\b(?:" + STORES + r"|cart|check-?out|out of stock|sold out|back-?order(?:ed)?|restock(?:ed|ing)?|re-?order(?:ed|ing|s)?"
                    r"|place (?:it|the order|this order|that order))\b", re.I)


def _and_join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1] if names else 'the other stores'


_EXAMPLE = (f'"{SHOPPING[0]} is out of the paper towel 12-pack. {SHOPPING[1]} has it, $24.98 total, here Thursday. Place it there?"'
            if len(SHOPPING) > 1 else '"The store is out of the paper towel 12-pack until Thursday. Want me to wait for it?"')
PARAGRAPH = (
    "If something the owner wants is out of stock, sold out or cannot be delivered at the store you are on: do not stop at telling them, "
    "and do not swap in a different product. Look for the same item (same brand, kind and size) at their other stores, "
    f"{_and_join(SHOPPING)}, by reading their pages. If one has it, get it ready there up to the order review page (that item only, no "
    "extras, never the Place order button), then ask them once, in one line: where it is out, where you found it, the full total there "
    f"and when it would come ({_EXAMPLE}). A yes they gave for the "
    "first store never covers another store or another total: nothing is bought at the second store until they say yes to that store and "
    "that total. If only a different size or brand is there, say that in the same line and let them pick. If none of them has it, say so "
    "in one line, with when the first store expects it back if its page says. Never look or buy anywhere else. "
    + (f"{FLIGHTS} is only for their flights: a sold-out flight gets the next {FLIGHTS} options with their totals, never another "
       "airline. " if FLIGHTS else ''))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return ' '.join(str(b.get('text') or '') for b in content if isinstance(b, dict) and b.get('type') in (None, 'text'))
    return ''


def about_buying(message, history=()) -> bool:
    """Is this turn about buying something? The owner's message, or one of the last few messages either side wrote (so a
    bare "yes" to "The order is $42.17, place it?" counts)."""
    try:
        if BUYING.search(str(message or '')[:4000]):
            return True
        recent = [m for m in list(history or [])[-LOOK_BACK:] if isinstance(m, dict) and m.get('role') in ('user', 'assistant')]
        return any(RECENT.search(_text(m.get('content'))[:4000]) for m in recent)
    except Exception:
        return False


def add(ctx: dict, message, history, before: str) -> dict:
    """-> the turn context with the paragraph (before the reference's closing sentence when that is there exactly once,
    else at the end), or the same context when this turn is not about buying."""
    if not about_buying(message, history):
        return ctx
    text = ctx.get('context') or ''
    if PARAGRAPH in text:
        return ctx
    if before and text.count(before) == 1:
        return dict(ctx, context=text.replace(before, PARAGRAPH + before))
    return dict(ctx, context=text + '\n' + PARAGRAPH.strip())
