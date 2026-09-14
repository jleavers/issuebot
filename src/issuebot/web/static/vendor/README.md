# Vendored front-end libraries

Pinned files, served from `/static/vendor/`; no CDN, no build step (roadmap decision 7). Bump
them by hand: download the new file, verify its checksum against the release, update this table,
the digest block below and the licence file beside it. Dependabot does not see these files;
`tests/test_web_vendor.py` parses this file and fails on a digest that does not match the bytes,
a version the file does not carry, or a file beside it this file does not record (#108).

| File | Library | Version | Source | Licence |
|---|---|---|---|---|
| `htmx.min.js` | htmx | 2.0.10 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js | 0BSD (`htmx.LICENSE`) |
| `chart.umd.js` | Chart.js | 4.5.1 | https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.js | MIT (`chart.LICENSE`) |

SHA-256 as vendored on 2026-09-04:

```
71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de  htmx.min.js
ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a  chart.umd.js
```
