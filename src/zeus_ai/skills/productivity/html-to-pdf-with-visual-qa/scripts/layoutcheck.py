#!/usr/bin/env python3
"""Measure a built chart.html in headless Chrome: boxes inside each page's padding, no clipped
text (scroll > client), no overlapping boxes, footer gap. Prints a per-page report.
Usage: python3 -I layoutcheck.py /path/to/chart.html
Calibrate first: run it on a version with a known defect and confirm it reports that defect.
A footer pinned in the bottom padding shows as 'OUTSIDE foot' by design; ignore that line only.
Adjust the selectors to your build's class names (.node, .card, .box, .flag, .foot, ...)."""
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(sys.argv[1]).resolve()
OUT = SRC.with_name("check-" + SRC.name)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CHECK_JS = r"""
<script>
window.addEventListener('load', function(){ setTimeout(function(){
  var out = [];
  function R(e){ return e.getBoundingClientRect(); }
  function label(e){
    var nm = e.querySelector && e.querySelector('.nm');
    var t = (nm ? nm.textContent : e.textContent || '').replace(/\s+/g,' ').trim().slice(0,40);
    return (e.className||e.tagName) + ' [' + t + ']';
  }
  document.querySelectorAll('section.page').forEach(function(pg, i){
    var P = R(pg), n = i+1, issues = 0, cs = getComputedStyle(pg);
    var inner = {l:P.left+parseFloat(cs.paddingLeft)-1, r:P.right-parseFloat(cs.paddingRight)+1,
                 t:P.top+parseFloat(cs.paddingTop)-1, b:P.bottom-parseFloat(cs.paddingBottom)+1};
    pg.querySelectorAll('.node, .card, .box, .flag, .lanes, .legend, .outside, .group, .stack, table, tr, .flow, .step, .foot, .hdr, h2, .cols, .col').forEach(function(e){
      var r = R(e);
      if (r.width === 0 && r.height === 0) return;
      if (r.left < inner.l || r.right > inner.r || r.top < inner.t || r.bottom > inner.b){
        out.push('P'+n+' OUTSIDE '+label(e)+' rect='+[r.left-P.left,r.top-P.top,r.right-P.left,r.bottom-P.top].map(Math.round).join(','));
        issues++;
      }
    });
    pg.querySelectorAll('.node, .card, .box, .flag, .lanes, td, .step').forEach(function(e){
      if (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1){
        out.push('P'+n+' CLIPPED '+label(e)); issues++;
      }
    });
    var boxes = Array.prototype.slice.call(pg.querySelectorAll('.node, .card, .flag, .lanes, .legend, .outside, .foot, .hdr, .flow, .bottom > .box'));
    for (var a = 0; a < boxes.length; a++) for (var b = a+1; b < boxes.length; b++){
      var A = boxes[a], B = boxes[b];
      if (A.contains(B) || B.contains(A)) continue;
      var ra = R(A), rb = R(B);
      var w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      var h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w > 1 && h > 1){ out.push('P'+n+' OVERLAP '+label(A)+' WITH '+label(B)); issues++; }
    }
    var foot = pg.querySelector('.foot');
    if (foot){
      var fr = R(foot), maxb = 0;
      pg.querySelectorAll('.node, .card, .box, .flag, table, .legend, .outside, .flows, .bottom, .cols').forEach(function(e){
        if (!foot.contains(e)) maxb = Math.max(maxb, R(e).bottom);
      });
      out.push('P'+n+' footer gap '+Math.round(fr.top - maxb)+'px');
    }
    out.push('P'+n+' issues '+issues);
  });
  var pre = document.createElement('pre'); pre.id = 'layout-report';
  pre.textContent = out.join('\n');
  document.body.appendChild(pre);
}, 600); });
</script>
"""

OUT.write_text(SRC.read_text(encoding="utf-8").replace("</body>", CHECK_JS + "</body>"), encoding="utf-8")
res = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--window-size=1100,900",
                      "--virtual-time-budget=5000", f"--user-data-dir={OUT.parent / '.chrome-check'}",
                      "--dump-dom", f"file://{OUT}"], capture_output=True, text=True, timeout=90)
m = re.search(r'<pre id="layout-report">(.*?)</pre>', res.stdout, re.S)
if not m:
    print("no report; chrome exit", res.returncode)
    sys.exit(1)
print(m.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
