Leaflet 1.9.4 (BSD-2-Clause, see LICENSE), `dist/leaflet.js` and `dist/leaflet.css` from the npm package
`leaflet@1.9.4` (npm verified the registry's sha512 integrity on download).

We serve these files ourselves instead of loading them from a CDN, so no third-party script runs on the page
(supply-chain risk). The page's Content-Security-Policy only allows scripts from our own origin.

sha256:
- leaflet.js  db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a
- leaflet.css a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6

To upgrade: `npm pack leaflet@<version>`, copy `package/dist/leaflet.{js,css}`, update the hashes here and in
`tests/test_web.py`.
