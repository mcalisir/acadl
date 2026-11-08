# sci-bulk-downloader

Bulk academic PDF downloader. Extracts DOIs from reference lists, downloads papers from Sci-Hub with automatic ALTCHA captcha solving, falls back to publisher open-access PDFs, and optionally queries the Unpaywall API for additional open-access sources.

## Features

- **DOI extraction** — paste a bibliography / reference list, DOIs are detected automatically
- **Sci-Hub download** — downloads from Sci-Hub with mirror support
- **ALTCHA captcha solver** — automatically solves Sci-Hub's proof-of-work captcha
- **Publisher fallback** — when Sci-Hub fails, resolves the DOI and finds the PDF on the publisher's site:
  - `citation_pdf_url` meta tag (works for most open-access publishers)
  - Frontiers URL pattern (`/full` → `/pdf`)
  - DergiPark download links
  - OJS-based journal repositories
- **Unpaywall API fallback** — when publisher scraping also fails, queries Unpaywall for open-access PDF URLs (covers MDPI `?version=` links, SAGE, Pegegog, and more)
- **Anti-bot stealth** — uses [Scrapling](https://github.com/AhmedKhalil777/scrapling) with Chrome TLS fingerprint impersonation
- **Clipboard support** — if no file is given, reads DOIs from clipboard (Windows)
- **Duplicate skip** — already downloaded PDFs are skipped

## Installation

```bash
pip install "scrapling[fetchers]"
```

Requires Python 3.10+.

## Usage

### From a reference file

```bash
python scihub_dl.py references.txt
```

The file can be a plain reference list (APA, MLA, etc.) — DOIs are extracted automatically.

### With Unpaywall fallback

```bash
python scihub_dl.py references.txt --email you@university.edu
```

Providing `--email` activates the Unpaywall API as a third fallback layer. Use a real email — Unpaywall rejects fake addresses.

### Single DOI

```bash
python scihub_dl.py --doi 10.1016/j.tate.2019.05.003
```

### Custom output directory

```bash
python scihub_dl.py references.txt -o ./papers
```

### From stdin

```bash
cat references.txt | python scihub_dl.py -
```

### List DOIs only (no download)

```bash
python scihub_dl.py references.txt --list
```

### Publisher-only mode (skip Sci-Hub)

```bash
python scihub_dl.py references.txt --no-scihub --email you@university.edu
```

### All options

```
usage: scihub_dl.py [-h] [--doi DOI] [--output OUTPUT] [--delay DELAY]
                    [--mirror MIRROR] [--list] [--no-scihub] [--email EMAIL]
                    [input]

positional arguments:
  input                 File containing DOIs (- = stdin)

options:
  -h, --help            show this help message and exit
  --doi DOI, -d DOI     Single DOI
  --output OUTPUT, -o OUTPUT
                        Download directory (default: ./downloads)
  --delay DELAY         Delay between requests in seconds (default: 5)
  --mirror MIRROR, -m MIRROR
                        Sci-Hub mirror URL (default: https://sci-hub.ist)
  --list                List DOIs only, don't download
  --no-scihub           Skip Sci-Hub, download from publisher only
  --email EMAIL, -e EMAIL
                        Email for Unpaywall API (enables third fallback)
```

## How it works

```
DOI → Sci-Hub → Publisher scraping → Unpaywall API → fail
```

1. **DOI extraction** — regex `10.\d{4,9}/...` picks up DOIs from any text
2. **Sci-Hub attempt** — fetches the Sci-Hub page, finds the embedded PDF iframe, downloads the PDF
3. **ALTCHA captcha** — if Sci-Hub shows a proof-of-work captcha, brute-forces the SHA-256 challenge and submits the solution
4. **Publisher fallback** — if Sci-Hub doesn't have the paper:
   - Resolves `https://doi.org/<doi>` to the publisher page
   - Looks for `citation_pdf_url` meta tag
   - Tries publisher-specific URL patterns (Frontiers, MDPI, DergiPark)
   - Scans page for download links
5. **Unpaywall fallback** (requires `--email`) — queries the Unpaywall API for open-access PDF URLs. Covers publishers where direct scraping fails.
6. **Validation** — checks `%PDF` magic bytes and minimum file size before saving

## Limitations

- **Elsevier/ScienceDirect** — blocks all automated access (403 Cloudflare), not open access
- **SAGE** — blocks automated access (403 Cloudflare), even Unpaywall URLs
- **MDPI** — Akamai bot protection blocks automated PDF downloads, even with meta-refresh redirect
- Some publishers require JavaScript rendering — this tool uses HTTP only, no browser automation
- Unpaywall covers open-access papers only; paywalled papers without Sci-Hub copies can't be downloaded

## Disclaimer

This tool is for personal academic use. Respect publishers' terms of service and your institution's policies. The responsibility for how this tool is used lies with the user.

## License

MIT
