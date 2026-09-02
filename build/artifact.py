#!/usr/bin/env python3
"""Assemble the static preview into one self-contained page for publishing.

The shareable page has no server behind it, so it uses the preview/ bundle —
a JavaScript port of macd_desk/charges.py, pinned to it by tests/test_parity.py.
The Artifact host supplies <!doctype>/<head>/<body> and its CSP blocks relative
stylesheets and scripts, so everything is inlined here.

Run: python build/artifact.py
"""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parent.parent
html = (root / 'preview' / 'index.html').read_text()
css = (root / 'macd_desk' / 'static' / 'styles.css').read_text()
js = '\n'.join((root / 'preview' / f).read_text() for f in ('charges.js', 'app.js'))

body = re.search(r'<body>(.*)</body>', html, re.S).group(1)
body = re.sub(r'\s*<script src="[^"]+"></script>', '', body)

fonts = ('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Mono:wght@400;500;600&'
         'family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">')

out = root / 'dist' / 'artifact.html'
out.parent.mkdir(exist_ok=True)
out.write_text(
    f'<title>Upstox MACD Options Desk</title>\n{fonts}\n<style>\n{css}</style>\n'
    f'{body.strip()}\n<script>\n{js}</script>\n'
)
print(f'wrote {out} ({out.stat().st_size // 1024} KB)')
