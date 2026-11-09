# acadl

Academic PDF downloader with a 4-layer fallback pipeline.  
Paste your DOIs, click download — it finds the PDFs for you.

**[Download the latest release](https://github.com/mcalisir/acadl/releases/latest)** — no installation needed, just extract and run.

---

## How it works

acadl tries four sources in order until it gets the PDF:

| Layer | Source | What it does |
|-------|--------|-------------|
| 1 | **Sci-Hub** | Finds the paper on Sci-Hub mirrors, auto-solves ALTCHA captchas |
| 2 | **Publisher** | Follows the DOI to the publisher's site and scrapes the open-access PDF |
| 3 | **Unpaywall** | Queries the Unpaywall API for legally free PDF links |
| 4 | **Stealth browser** | Launches a headless Chromium to bypass Cloudflare/Akamai bot protection |

If all four fail, the paper is likely paywalled without any open-access copy available.

---

## Quick start

### Windows exe (recommended)

1. Download **acadl.zip** from [Releases](https://github.com/mcalisir/acadl/releases/latest)
2. Extract the zip
3. Double-click **acadl.exe** — the GUI opens
4. Paste your DOIs, pick an output folder, click **Start Download**

> Windows may show a SmartScreen warning (the exe is not code-signed).  
> Click **More info** → **Run anyway**.

### From source

```bash
pip install "scrapling[fetchers]"
python acadl.py refs.txt --stealth --email you@university.edu
```

Requires Python 3.10+.

---

## GUI

The GUI launches when you double-click the exe (or run without arguments).

- Paste DOIs directly or load a `.txt` file
- Choose output folder
- Toggle Sci-Hub, stealth browser, Unpaywall
- Set parallel workers (1–8, default 4)
- Watch progress in the log area

---

## CLI usage

```bash
# Basic — download DOIs from a file
acadl.exe refs.txt

# Single DOI
acadl.exe --doi 10.1016/j.tate.2019.05.003

# All 4 layers enabled with 8 parallel workers
acadl.exe refs.txt --stealth --email you@university.edu -p 8

# Custom output folder
acadl.exe refs.txt -o ./papers

# List extracted DOIs without downloading
acadl.exe refs.txt --list

# Skip Sci-Hub (publisher + Unpaywall + stealth only)
acadl.exe refs.txt --no-scihub --email you@university.edu --stealth

# Read from stdin
type refs.txt | acadl.exe -
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--doi`, `-d` | Single DOI to download | — |
| `--output`, `-o` | Output folder | `./downloads` |
| `--email`, `-e` | Email for Unpaywall API (enables layer 3) | — |
| `--stealth` | Enable stealth browser (layer 4) | off |
| `--parallel`, `-p` | Parallel workers | 4 |
| `--delay` | Delay between requests (seconds) | 1 |
| `--mirror`, `-m` | Sci-Hub mirror URL | `https://sci-hub.ist` |
| `--no-scihub` | Skip Sci-Hub entirely | off |
| `--list` | List DOIs only, don't download | off |

---

## Supported publishers

Works well with most open-access and hybrid publishers:

- **Frontiers**, **MDPI**, **SpringerOpen**, **Wiley OA** — direct PDF links
- **DergiPark** — Turkish journal repository
- **OJS journals** — Open Journal Systems repositories
- **PeGeGoG**, **IJATE** and other academic platforms
- **Any publisher** with a `citation_pdf_url` meta tag

Limited or no access:
- **Elsevier/ScienceDirect** — heavy Cloudflare protection, stealth browser helps sometimes
- **SAGE** — Cloudflare + Akamai, stealth browser may bypass
- Fully paywalled papers without any OA copy cannot be downloaded

---

## Disclaimer

This tool is for personal academic use. Respect publishers' terms of service and your institution's policies. The responsibility for how this tool is used lies with the user.

## License

MIT
