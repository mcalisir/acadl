"""
acadl - Academic PDF Downloader

4-layer download pipeline: Sci-Hub → Publisher scraping → Unpaywall API → Stealth browser.

Usage:
    python acadl.py refs.txt                           # extract DOIs from file and download
    python acadl.py --doi 10.1016/j.tate.2019.05.003  # single DOI
    python acadl.py refs.txt -o ./papers               # specify output folder
    type refs.txt | python acadl.py -                  # read from stdin
    python acadl.py refs.txt --no-scihub               # publisher only
    python acadl.py refs.txt --email a@b.edu           # enable Unpaywall fallback
    python acadl.py refs.txt --stealth                 # stealth browser (4th layer)

Requirements:
    pip install "scrapling[fetchers]"
"""

import re
import os
import sys
import time
import json
import hashlib
import base64
import shutil
import tempfile
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from scrapling.fetchers import FetcherSession
except ImportError:
    sys.exit('scrapling eksik. Kur: pip install "scrapling[fetchers]"')


# --- Ayarlar ----------------------------------------------------------------

SCIHUB_BASE = "https://sci-hub.ist"
DELAY_SEC = 1
PARALLEL_WORKERS = 4
MAX_RETRIES = 2

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;)\]}>\"']+[^\s,;.)\]}>\"':]")


# --- Yardimci ---------------------------------------------------------------

def extract_dois(text: str) -> list[str]:
    """Metinden benzersiz DOI listesi cikarir (sira korunur)."""
    raw = DOI_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for doi in raw:
        doi = doi.rstrip(".,;:")
        if doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def safe_filename(doi: str) -> str:
    return doi.replace("/", "__").replace(":", "_") + ".pdf"


def normalize_url(url: str, base: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    if not url.startswith("http"):
        return base.rstrip("/") + "/" + url
    return url


# --- Sayfa analiz -----------------------------------------------------------

def find_pdf_url(resp) -> str | None:
    """Scrapling Response nesnesinden PDF URL'sini bulur."""

    # 1) iframe#pdf (Sci-Hub klasik)
    sel = resp.css("iframe#pdf")
    if sel:
        src = sel.first.attrib.get("src")
        if src:
            return src

    # 2) herhangi bir iframe'in src'sinde .pdf (Sci-Net mirror)
    for iframe in resp.css("iframe"):
        src = iframe.attrib.get("src", "")
        if ".pdf" in src.lower():
            src = src.split("#")[0]
            return src

    # 3) embed
    for q in ("embed[type='application/pdf']", "embed#pdf"):
        sel = resp.css(q)
        if sel:
            src = sel.first.attrib.get("src")
            if src:
                return src

    # 4) onclick yonlendirmesi (#buttons a)
    sel = resp.css("#buttons a")
    if sel:
        onclick = sel.first.attrib.get("onclick", "")
        m = re.search(r"location\.href\s*=\s*'([^']+)'", onclick)
        if m:
            return m.group(1)

    # 5) herhangi bir .pdf href
    for a in resp.css("a"):
        href = a.attrib.get("href", "")
        if ".pdf" in href.lower():
            return href

    # 6) raw HTML'de src="...pdf" (fallback)
    text = resp.body.decode("utf-8", errors="replace")
    m = re.search(r'src\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']', text, re.I)
    if m:
        return m.group(1).split("#")[0]

    return None


def has_altcha(resp) -> bool:
    """Sayfada ALTCHA proof-of-work captcha var mi?"""
    text = resp.body.decode("utf-8", errors="replace")
    return "captcha/challenge" in text


def solve_altcha(resp, session, base: str) -> bool:
    """ALTCHA proof-of-work captcha'yi cozer. Basarili ise True doner."""
    text = resp.body.decode("utf-8", errors="replace")

    m_ch = re.search(r'challengeurl\s*=\s*"([^"]+)"', text)
    m_sol = re.search(r"fetch\s*\(\s*'(/captcha/solution/[^']+)'", text)
    if not m_ch or not m_sol:
        return False

    challenge_path = m_ch.group(1)
    solution_path = m_sol.group(1)

    cr = session.get(f"{base}{challenge_path}")
    try:
        ch = json.loads(cr.body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False

    salt = ch.get("salt", "")
    target = ch.get("challenge", "")
    max_n = ch.get("maxNumber", 200000)

    solution_n = None
    for n in range(max_n + 1):
        h = hashlib.sha256((salt + str(n)).encode()).hexdigest()
        if h == target:
            solution_n = n
            break

    if solution_n is None:
        return False

    payload = base64.b64encode(json.dumps({
        "algorithm": ch.get("algorithm", "SHA-256"),
        "challenge": target,
        "number": solution_n,
        "salt": salt,
        "signature": ch.get("signature", ""),
    }).encode()).decode()

    sr = session.post(
        f"{base}{solution_path}",
        json={"captcha": payload},
        headers={"Content-Type": "application/json"},
    )

    try:
        result = json.loads(sr.body.decode())
        return result.get("success", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def is_not_available(resp) -> bool:
    """Sci-Hub'da makale mevcut degil mi?"""
    text = resp.body.decode("utf-8", errors="replace").lower()
    return any(kw in text for kw in (
        "not found</title>",
        "не найдена",
        "not available through sci-hub",
        "article is not available",
        "the article is not available",
    ))


# --- Yayinci fallback (acik erisim) ----------------------------------------

def find_publisher_pdf(doi: str, session) -> str | None:
    """DOI'yi cozumleyip yayinci sayfasindan PDF URL'si bulur."""
    try:
        resp = session.get(f"https://doi.org/{doi}")
    except Exception:
        return None

    final_url = str(getattr(resp, 'url', ''))
    if resp.status >= 400:
        return None

    text = resp.body.decode("utf-8", errors="replace")

    # 1) citation_pdf_url meta etiketi (en guvenilir)
    for pattern in (
        r'<meta[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']',
    ):
        m = re.search(pattern, text, re.I)
        if m:
            url = m.group(1)
            if url.startswith("/"):
                base = re.match(r'https?://[^/]+', final_url)
                url = (base.group(0) if base else "") + url
            return url

    # 2) Frontiers: /full -> /pdf
    if "frontiersin.org" in final_url and "/full" in final_url:
        return final_url.replace("/full", "/pdf")

    # 3) MDPI: makale URL'si + /pdf
    if "mdpi.com" in final_url:
        candidate = final_url.rstrip("/") + "/pdf"
        try:
            r2 = session.get(candidate)
            if r2.body[:5].startswith(b"%PDF"):
                return candidate
        except Exception:
            pass

    # 4) Sayfadaki herhangi bir download/pdf linki
    for a in resp.css("a"):
        href = a.attrib.get("href", "")
        txt = (a.text or "").lower()
        if ("download" in href.lower() and "pdf" in txt) or \
           ("download" in txt and ".pdf" in href.lower()) or \
           ("/download/article-file/" in href):
            if href.startswith("/"):
                base = re.match(r'https?://[^/]+', final_url)
                href = (base.group(0) if base else "") + href
            return href

    return None


def download_from_publisher(doi: str, output_dir: Path, session, idx: int, total: int) -> bool:
    """Yayinci sitesinden acik erisimli PDF indir."""
    filename = safe_filename(doi)
    filepath = output_dir / filename

    print(f"           >> Yayinci fallback deneniyor...")
    pdf_url = find_publisher_pdf(doi, session)
    if not pdf_url:
        print(f"           X  No PDF found on publisher page")
        return False

    print(f"           PDF: {pdf_url[:90]}...")
    try:
        resp = session.get(pdf_url)
        raw = resp.body

        if len(raw) < 1000:
            print(f"           !  Dosya cok kucuk ({len(raw)} B)")
            return False

        if not raw[:5].startswith(b"%PDF"):
            print(f"           !  Not a valid PDF")
            return False

        filepath.write_bytes(raw)
        kb = len(raw) / 1024
        print(f"           OK {filename}  ({kb:.0f} KB) [publisher]")
        return True
    except Exception as e:
        print(f"           !  Publisher download error: {e}")
        return False


# --- Unpaywall API fallback -------------------------------------------------

UNPAYWALL_API = "https://api.unpaywall.org/v2"

def find_unpaywall_pdf(doi: str, session, email: str) -> str | None:
    """Unpaywall API'den acik erisim PDF URL'si bulur."""
    try:
        url = f"{UNPAYWALL_API}/{doi}?email={email}"
        resp = session.get(url)
        if resp.status != 200:
            return None
        data = json.loads(resp.body.decode())
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf")
        if pdf_url:
            return pdf_url
        for loc in data.get("oa_locations", []):
            if loc.get("url_for_pdf"):
                return loc["url_for_pdf"]
        return None
    except Exception:
        return None


def _follow_meta_refresh(body: bytes, base_url: str) -> str | None:
    """HTML'deki meta http-equiv=refresh redirect URL'sini cikar."""
    text = body[:4000].decode("utf-8", errors="replace")
    m = re.search(
        r'http-equiv=["\']refresh["\'][^>]*content=["\'][^;]*;\s*URL=[\'\"]?([^"\'>\s]+)',
        text, re.I,
    )
    if not m:
        return None
    url = m.group(1)
    if url.startswith("/"):
        base = re.match(r'https?://[^/]+', base_url)
        url = (base.group(0) if base else "") + url
    return url


def download_from_unpaywall(doi: str, output_dir: Path, session, email: str,
                             idx: int, total: int) -> bool:
    """Unpaywall API uzerinden acik erisimli PDF indir."""
    filename = safe_filename(doi)
    filepath = output_dir / filename

    print(f"           >> Unpaywall API deneniyor...")
    pdf_url = find_unpaywall_pdf(doi, session, email)
    if not pdf_url:
        print(f"           X  No open-access PDF found on Unpaywall")
        return False

    print(f"           PDF: {pdf_url[:90]}...")
    try:
        resp = session.get(pdf_url)
        raw = resp.body

        # Meta-refresh redirect varsa takip et
        if not raw[:5].startswith(b"%PDF") and b"http-equiv" in raw[:4000]:
            redirect = _follow_meta_refresh(raw, pdf_url)
            if redirect:
                print(f"           >> Meta-refresh redirect takip ediliyor...")
                resp = session.get(redirect)
                raw = resp.body

        if len(raw) < 1000:
            print(f"           !  Dosya cok kucuk ({len(raw)} B)")
            return False

        if not raw[:5].startswith(b"%PDF"):
            print(f"           !  Not a valid PDF (possible bot protection)")
            return False

        filepath.write_bytes(raw)
        kb = len(raw) / 1024
        print(f"           OK {filename}  ({kb:.0f} KB) [unpaywall]")
        return True
    except Exception as e:
        print(f"           !  Unpaywall download error: {e}")
        return False


# --- Stealth browser fallback (Cloudflare/Akamai bypass) -------------------

def _stealth_download_pdf(pdf_url: str, output_path: Path) -> bool:
    """StealthyFetcher ile PDF indir. Browser download event'ini yakalar."""
    dl_dir = tempfile.mkdtemp(prefix="stealth_dl_")
    saved = [None]

    def on_setup(page):
        def handle(download):
            path = os.path.join(dl_dir, download.suggested_filename or "paper.pdf")
            try:
                download.save_as(path)
            except Exception:
                pass
            saved[0] = path
        page.on("download", handle)

    try:
        from scrapling.fetchers import StealthyFetcher
        r = StealthyFetcher.fetch(
            pdf_url, headless=True, solve_cloudflare=True, page_setup=on_setup)
        if r.body[:5].startswith(b"%PDF") and len(r.body) > 1000:
            output_path.write_bytes(r.body)
            return True
    except Exception:
        pass

    # Download handler yakaladiysa kontrol et
    for f in ([saved[0]] if saved[0] else []) + \
             [os.path.join(dl_dir, x) for x in os.listdir(dl_dir)]:
        if f and os.path.exists(f) and os.path.getsize(f) > 1000:
            try:
                with open(f, "rb") as fh:
                    if fh.read(5) == b"%PDF-":
                        shutil.copy2(f, output_path)
                        return True
            except Exception:
                pass

    shutil.rmtree(dl_dir, ignore_errors=True)
    return False


def download_with_stealth(doi: str, output_dir: Path, session,
                           idx: int, total: int) -> bool:
    """4. fallback: StealthyFetcher ile bot korumali siteleri as."""
    filename = safe_filename(doi)
    filepath = output_dir / filename
    print(f"           >> Stealth browser deneniyor...")

    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        print(f"           !  StealthyFetcher yuklenemedi")
        return False

    # 1) DOI'yi cozumle, makale sayfasini stealth browser ile ac
    try:
        resp = StealthyFetcher.fetch(
            f"https://doi.org/{doi}", headless=True, solve_cloudflare=True)
    except Exception as e:
        print(f"           !  Makale sayfasi acilamadi: {e}")
        return False

    text = resp.body.decode("utf-8", errors="replace")

    # 2) citation_pdf_url bul
    pdf_url = None
    for pat in (
        r'<meta[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']',
    ):
        m = re.search(pat, text, re.I)
        if m:
            pdf_url = m.group(1)
            break

    if not pdf_url:
        # MDPI, Frontiers vb. icin URL pattern dene
        final_url = str(getattr(resp, 'url', ''))
        if "mdpi.com" in final_url:
            pdf_url = final_url.rstrip("/") + "/pdf"
        elif "frontiersin.org" in final_url and "/full" in final_url:
            pdf_url = final_url.replace("/full", "/pdf")

    if not pdf_url:
        print(f"           X  No PDF link found via stealth browser")
        return False

    print(f"           PDF: {pdf_url[:90]}...")

    # 3) PDF'i browser uzerinden indir (download handler ile)
    if _stealth_download_pdf(pdf_url, filepath):
        kb = filepath.stat().st_size / 1024
        print(f"           OK {filename}  ({kb:.0f} KB) [stealth]")
        return True

    print(f"           X  Stealth download failed")
    return False


# --- Indirme motoru ---------------------------------------------------------

def download_one(doi: str, output_dir: Path, session, idx: int, total: int,
                  base: str, use_scihub: bool = True) -> bool:
    filename = safe_filename(doi)
    filepath = output_dir / filename

    if filepath.exists():
        print(f"  [{idx}/{total}] >> Zaten var: {filename}")
        return True

    print(f"  [{idx}/{total}] {doi}")

    if not use_scihub:
        return download_from_publisher(doi, output_dir, session, idx, total)

    url = f"{base}/{doi}"
    print(f"           {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url)

            # Dogrudan PDF dondu mu?
            ct = resp.headers.get("content-type", "")
            if "application/pdf" in ct and resp.body[:5].startswith(b"%PDF"):
                filepath.write_bytes(resp.body)
                kb = len(resp.body) / 1024
                print(f"           OK (dogrudan PDF) {filename}  ({kb:.0f} KB)")
                return True

            # Makale Sci-Hub'da mevcut degil mi?
            if is_not_available(resp):
                print("           X  Sci-Hub'da mevcut degil")
                return False

            # Once PDF linkini ara (captcha sayfasinda bile gomulu olabilir)
            pdf_link = find_pdf_url(resp)

            # PDF yoksa ve ALTCHA captcha varsa coz, tekrar dene
            if not pdf_link and has_altcha(resp):
                print("           ALTCHA captcha algilandi, cozuluyor...")
                if solve_altcha(resp, session, base):
                    print("           Captcha cozuldu, sayfa yeniden aliniyor...")
                    resp = session.get(url)
                    ct = resp.headers.get("content-type", "")
                    if "application/pdf" in ct and resp.body[:5].startswith(b"%PDF"):
                        filepath.write_bytes(resp.body)
                        kb = len(resp.body) / 1024
                        print(f"           OK {filename}  ({kb:.0f} KB)")
                        return True
                    if is_not_available(resp):
                        print("           X  Sci-Hub'da mevcut degil")
                        return False
                    pdf_link = find_pdf_url(resp)
                else:
                    print("           !  Captcha cozulemedi")

            if not pdf_link:
                if attempt < MAX_RETRIES:
                    print(f"           !  PDF linki yok, tekrar ({attempt}/{MAX_RETRIES})...")
                    time.sleep(3)
                    continue
                print("           X  No PDF link found")
                return False

            resp_url = str(getattr(resp, 'url', url))
            resp_base = re.match(r'https?://[^/]+', resp_url)
            effective_base = resp_base.group(0) if resp_base else base
            pdf_url = normalize_url(pdf_link, effective_base)
            print(f"           PDF: {pdf_url[:90]}...")

            resp_pdf = session.get(pdf_url)

            raw = resp_pdf.body
            if len(raw) < 1000:
                print(f"           !  Dosya cok kucuk ({len(raw)} B)")
                if attempt < MAX_RETRIES:
                    time.sleep(3)
                    continue

            if not raw[:5].startswith(b"%PDF"):
                print(f"           !  Not a valid PDF (first bytes: {raw[:15]})")
                if attempt < MAX_RETRIES:
                    time.sleep(3)
                    continue
                print("           X  Could not download a valid PDF")
                return False

            filepath.write_bytes(raw)
            kb = len(raw) / 1024
            print(f"           OK {filename}  ({kb:.0f} KB)")
            return True

        except Exception as e:
            print(f"           !  Hata: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(3)
            else:
                print(f"           X  Basarisiz ({MAX_RETRIES} deneme)")
                return False

    return False


# --- Ana akis ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="acadl - Academic PDF Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python acadl.py refs.txt
  python acadl.py --doi 10.1016/j.tate.2019.05.003
  python acadl.py refs.txt -o ./papers
  type refs.txt | python acadl.py -
  python acadl.py --list refs.txt
""",
    )
    parser.add_argument("input", nargs="?", help="File containing DOIs (- = stdin)")
    parser.add_argument("--doi", "-d", help="Single DOI")
    parser.add_argument("--output", "-o", default="./downloads",
                        help="Output folder (default: ./downloads)")
    parser.add_argument("--delay", type=int, default=DELAY_SEC,
                        help=f"Delay between requests in seconds (default: {DELAY_SEC})")
    parser.add_argument("--mirror", "-m", default=SCIHUB_BASE,
                        help=f"Sci-Hub mirror (default: {SCIHUB_BASE})")
    parser.add_argument("--list", action="store_true",
                        help="Only list extracted DOIs, no download")
    parser.add_argument("--no-scihub", action="store_true",
                        help="Skip Sci-Hub, download from publisher only")
    parser.add_argument("--email", "-e", default=None,
                        help="Email for Unpaywall API (enables 3rd fallback layer)")
    parser.add_argument("--stealth", action="store_true",
                        help="Stealth browser fallback for bot-protected sites (4th layer)")
    parser.add_argument("--parallel", "-p", type=int, default=PARALLEL_WORKERS,
                        help=f"Parallel download workers (default: {PARALLEL_WORKERS})")

    args = parser.parse_args()

    # --- DOI toplama ---
    dois: list[str] = []

    if args.doi:
        dois = [args.doi.strip()]
    elif args.input == "-":
        dois = extract_dois(sys.stdin.read())
    elif args.input:
        p = Path(args.input)
        if not p.exists():
            sys.exit(f"File not found: {args.input}")
        dois = extract_dois(p.read_text(encoding="utf-8"))
    else:
        try:
            import subprocess
            cp = subprocess.run(
                ["powershell", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5,
            )
            if cp.returncode == 0 and cp.stdout.strip():
                dois = extract_dois(cp.stdout)
                if dois:
                    print(f"Detected {len(dois)} DOIs from clipboard.\n")
        except Exception:
            pass
        if not dois:
            parser.print_help()
            sys.exit(1)

    if not dois:
        sys.exit("No DOIs found.")

    # --- Sadece listele ---
    if args.list:
        for i, d in enumerate(dois, 1):
            print(f"  {i:2d}. {d}")
        print(f"\nTotal: {len(dois)} DOIs")
        return

    # --- Hazirlik ---
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = args.mirror.rstrip("/")

    sep = "=" * 60
    print(sep)
    print(f"  acadl - Academic PDF Downloader")
    if args.no_scihub:
        print(f"  Mode    : Publisher only (Sci-Hub skipped)")
    else:
        print(f"  Mirror  : {base}")
        print(f"  Fallback: Publisher site (open access)")
    if args.email:
        print(f"  Unpaywall: ON ({args.email})")
    else:
        print(f"  Unpaywall: off (enable with --email)")
    if args.stealth:
        print(f"  Stealth  : ON (Cloudflare/Akamai bypass)")
    else:
        print(f"  Stealth  : off (enable with --stealth)")
    print(f"  Parallel: {args.parallel} workers")
    print(f"  DOIs   : {len(dois)}")
    print(f"  Folder : {output_dir.resolve()}")
    print(f"  Delay  : {args.delay}s")
    print(sep + "\n")

    for i, d in enumerate(dois, 1):
        print(f"  {i:2d}. {d}")
    print()

    # --- Indirme ---
    ok = 0
    fail: list[str] = []
    print_lock = threading.Lock()

    def _download_doi(idx_doi):
        i, doi = idx_doi
        with FetcherSession(impersonate="chrome", stealthy_headers=True, timeout=30) as session:
            success = False
            if args.no_scihub:
                success = download_one(doi, output_dir, session, i, len(dois),
                                       base, use_scihub=False)
            else:
                success = download_one(doi, output_dir, session, i, len(dois), base)
                if not success:
                    success = download_from_publisher(
                        doi, output_dir, session, i, len(dois))
            if not success and args.email:
                success = download_from_unpaywall(
                    doi, output_dir, session, args.email, i, len(dois))
            if not success and args.stealth:
                success = download_with_stealth(
                    doi, output_dir, session, i, len(dois))
        return doi, success

    workers = min(args.parallel, len(dois))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for idx_doi in enumerate(dois, 1):
            f = pool.submit(_download_doi, idx_doi)
            futures[f] = idx_doi[1]
            time.sleep(args.delay)

        for f in as_completed(futures):
            doi, success = f.result()
            if success:
                ok += 1
            else:
                fail.append(doi)

    # --- Summary ---
    print(f"\n{sep}")
    print(f"  SUMMARY")
    print(f"  Success: {ok}/{len(dois)}")
    if fail:
        print(f"  Failed : {len(fail)}")
        for d in fail:
            print(f"     - {d}")
        if not args.email:
            print(f"\n  TIP: Enable Unpaywall fallback with --email")
    print(f"  Folder : {output_dir.resolve()}")
    print(sep)


def launch_gui() -> None:
    """Tkinter GUI — exe cift tiklaninca acilar."""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext
    import threading

    root = tk.Tk()
    root.title("acadl - Academic PDF Downloader")
    root.geometry("780x700")
    root.resizable(True, True)

    style = ttk.Style()
    style.configure("TButton", padding=5, font=("Segoe UI", 10))
    style.configure("TLabel", padding=2, font=("Segoe UI", 10))
    style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("TCheckbutton", font=("Segoe UI", 10))

    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill=tk.BOTH, expand=True)

    # --- DOI Girisi (cok satirli) ---
    doi_header = ttk.Frame(main_frame)
    doi_header.pack(fill=tk.X)
    ttk.Label(doi_header, text="Paste / Enter DOIs", style="Header.TLabel").pack(side=tk.LEFT)

    def browse_file():
        path = filedialog.askopenfilename(
            title="Select DOI list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            doi_text.delete("1.0", tk.END)
            doi_text.insert("1.0", content)

    ttk.Button(doi_header, text="Load from File", command=browse_file).pack(side=tk.RIGHT)

    doi_text = tk.Text(main_frame, height=6, font=("Consolas", 10), wrap=tk.WORD)
    doi_text.pack(fill=tk.X, pady=(4, 8))
    doi_text.insert("1.0", "Paste DOIs here (one per line or mixed text)...")
    doi_text.bind("<FocusIn>", lambda e: _clear_placeholder(e, doi_text))

    def _clear_placeholder(event, widget):
        if widget.get("1.0", "end-1c").startswith("Paste DOIs here"):
            widget.delete("1.0", tk.END)

    # --- Cikti Klasoru ---
    out_frame = ttk.Frame(main_frame)
    out_frame.pack(fill=tk.X, pady=4)
    ttk.Label(out_frame, text="Output Folder:").pack(side=tk.LEFT)
    out_entry = ttk.Entry(out_frame, font=("Segoe UI", 10))
    out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
    out_entry.insert(0, str(Path.home() / "Downloads" / "pdfs"))

    def browse_out():
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            out_entry.delete(0, tk.END)
            out_entry.insert(0, path)

    ttk.Button(out_frame, text="...", width=3, command=browse_out).pack(side=tk.RIGHT)

    # --- Secenekler ---
    ttk.Label(main_frame, text="Options", style="Header.TLabel").pack(anchor=tk.W, pady=(8, 0))

    opts_frame = ttk.Frame(main_frame)
    opts_frame.pack(fill=tk.X, pady=4)

    stealth_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts_frame, text="Stealth Browser (4th layer)",
                     variable=stealth_var).pack(side=tk.LEFT, padx=(0, 20))

    no_scihub_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts_frame, text="Skip Sci-Hub",
                     variable=no_scihub_var).pack(side=tk.LEFT, padx=(0, 20))

    row2 = ttk.Frame(main_frame)
    row2.pack(fill=tk.X, pady=4)
    ttk.Label(row2, text="Unpaywall Email:").pack(side=tk.LEFT)
    email_entry = ttk.Entry(row2, width=28, font=("Segoe UI", 10))
    email_entry.pack(side=tk.LEFT, padx=6)
    ttk.Label(row2, text="Mirror:").pack(side=tk.LEFT, padx=(12, 0))
    mirror_entry = ttk.Entry(row2, width=22, font=("Segoe UI", 10))
    mirror_entry.pack(side=tk.LEFT, padx=6)
    mirror_entry.insert(0, SCIHUB_BASE)

    row3 = ttk.Frame(main_frame)
    row3.pack(fill=tk.X, pady=4)
    ttk.Label(row3, text="Parallel Workers:").pack(side=tk.LEFT)
    parallel_spin = tk.Spinbox(row3, from_=1, to=8, width=4, font=("Segoe UI", 10))
    parallel_spin.pack(side=tk.LEFT, padx=6)
    parallel_spin.delete(0, tk.END)
    parallel_spin.insert(0, str(PARALLEL_WORKERS))

    # --- Butonlar ---
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill=tk.X, pady=(8, 4))

    running = threading.Event()

    def log(msg: str):
        log_text.configure(state=tk.NORMAL)
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
        log_text.configure(state=tk.DISABLED)

    def run_download():
        if running.is_set():
            return
        running.set()
        start_btn.configure(state=tk.DISABLED)
        log_text.configure(state=tk.NORMAL)
        log_text.delete("1.0", tk.END)
        log_text.configure(state=tk.DISABLED)

        raw = doi_text.get("1.0", "end-1c").strip()
        out_dir = Path(out_entry.get().strip())
        email_val = email_entry.get().strip() or None
        mirror_val = mirror_entry.get().strip() or SCIHUB_BASE
        use_stealth = stealth_var.get()
        skip_scihub = no_scihub_var.get()

        dois = extract_dois(raw)
        if not dois:
            log("ERROR: No DOIs found. Paste DOIs or load a file.")
            running.clear()
            start_btn.configure(state=tk.NORMAL)
            return

        n_workers = int(parallel_spin.get() or PARALLEL_WORKERS)
        out_dir.mkdir(parents=True, exist_ok=True)
        log(f"{'='*50}")
        log(f"  {len(dois)} DOIs to download -> {out_dir.resolve()}")
        log(f"  Parallel: {n_workers} workers")
        if use_stealth:
            log(f"  Stealth: ON")
        if skip_scihub:
            log(f"  Sci-Hub: SKIPPED")
        if email_val:
            log(f"  Unpaywall: {email_val}")
        log(f"{'='*50}\n")

        def worker():
            old_stdout = sys.stdout
            old_stderr = sys.stderr

            class GUIWriter:
                def write(self, s):
                    if s and s.strip():
                        root.after(0, log, s.rstrip("\n"))
                def flush(self):
                    pass

            writer = GUIWriter()
            sys.stdout = writer
            sys.stderr = writer

            try:
                ok = 0
                fail_list: list[str] = []

                def _dl(idx_doi):
                    i, doi = idx_doi
                    with FetcherSession(impersonate="chrome", stealthy_headers=True, timeout=30) as session:
                        success = False
                        if skip_scihub:
                            success = download_one(doi, out_dir, session, i, len(dois),
                                                   mirror_val, use_scihub=False)
                        else:
                            success = download_one(doi, out_dir, session, i, len(dois), mirror_val)
                            if not success:
                                success = download_from_publisher(
                                    doi, out_dir, session, i, len(dois))
                        if not success and email_val:
                            success = download_from_unpaywall(
                                doi, out_dir, session, email_val, i, len(dois))
                        if not success and use_stealth:
                            success = download_with_stealth(
                                doi, out_dir, session, i, len(dois))
                    return doi, success

                w = min(n_workers, len(dois))
                with ThreadPoolExecutor(max_workers=w) as pool:
                    futures = {}
                    for idx_doi in enumerate(dois, 1):
                        f = pool.submit(_dl, idx_doi)
                        futures[f] = idx_doi[1]
                        time.sleep(DELAY_SEC)
                    for f in as_completed(futures):
                        doi, success = f.result()
                        if success:
                            ok += 1
                        else:
                            fail_list.append(doi)

                root.after(0, log, f"\n{'='*50}")
                root.after(0, log, f"  SUMMARY: {ok}/{len(dois)} successful")
                if fail_list:
                    root.after(0, log, f"  Failed: {len(fail_list)}")
                    for d in fail_list:
                        root.after(0, log, f"    - {d}")
                root.after(0, log, f"  Folder: {out_dir.resolve()}")
                root.after(0, log, f"{'='*50}")
            except Exception as e:
                root.after(0, log, f"\nERROR: {e}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                running.clear()
                root.after(0, lambda: start_btn.configure(state=tk.NORMAL))

        threading.Thread(target=worker, daemon=True).start()

    start_btn = ttk.Button(btn_frame, text="Start Download", command=run_download)
    start_btn.pack(side=tk.LEFT, padx=(0, 10))

    def open_folder():
        d = out_entry.get().strip()
        if os.path.isdir(d):
            os.startfile(d)

    ttk.Button(btn_frame, text="Open Folder", command=open_folder).pack(side=tk.LEFT)

    # --- Log Alani ---
    ttk.Label(main_frame, text="Output", style="Header.TLabel").pack(anchor=tk.W, pady=(8, 0))
    log_text = scrolledtext.ScrolledText(main_frame, height=12, state=tk.DISABLED,
                                          font=("Consolas", 10))
    log_text.pack(fill=tk.BOTH, expand=True, pady=4)

    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) <= 1:
        try:
            launch_gui()
        except Exception as e:
            log_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else '.', 'crash.log')
            with open(log_path, 'w', encoding='utf-8') as f:
                import traceback
                traceback.print_exc(file=f)
    else:
        main()
