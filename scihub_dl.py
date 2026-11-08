"""
scihub_dl.py - Akademik PDF Toplu Indirici (Scrapling)

Sci-Hub'dan indirir; bulamazsa yayincinin sitesinden acik erisimli PDF'i alir.

Kullanim:
    python scihub_dl.py kaynaklar.txt                    # dosyadan DOI cikar ve indir
    python scihub_dl.py --doi 10.1016/j.tate.2019.05.003  # tek DOI
    python scihub_dl.py kaynaklar.txt -o ./makaleler       # klasor belirt
    type kaynaklar.txt | python scihub_dl.py -             # stdin'den oku
    python scihub_dl.py kaynaklar.txt --no-scihub          # sadece yayinci

Gereksinimler:
    pip install "scrapling[fetchers]"
"""

import re
import sys
import time
import json
import hashlib
import base64
import argparse
from pathlib import Path

try:
    from scrapling.fetchers import FetcherSession
except ImportError:
    sys.exit('scrapling eksik. Kur: pip install "scrapling[fetchers]"')


# --- Ayarlar ----------------------------------------------------------------

SCIHUB_BASE = "https://sci-hub.ist"
DELAY_SEC = 5
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
        print(f"           X  Yayici sayfasinda da PDF bulunamadi")
        return False

    print(f"           PDF: {pdf_url[:90]}...")
    try:
        resp = session.get(pdf_url)
        raw = resp.body

        if len(raw) < 1000:
            print(f"           !  Dosya cok kucuk ({len(raw)} B)")
            return False

        if not raw[:5].startswith(b"%PDF"):
            print(f"           !  Gecerli PDF degil")
            return False

        filepath.write_bytes(raw)
        kb = len(raw) / 1024
        print(f"           OK {filename}  ({kb:.0f} KB) [yayinci]")
        return True
    except Exception as e:
        print(f"           !  Yayinci indirme hatasi: {e}")
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
                print("           X  PDF linki bulunamadi")
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
                print(f"           !  Gecerli PDF degil (ilk byte: {raw[:15]})")
                if attempt < MAX_RETRIES:
                    time.sleep(3)
                    continue
                print("           X  Gecerli PDF indirilemedi")
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
        description="Sci-Hub DOI Toplu PDF Indirici (Scrapling)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Ornekler:
  python scihub_dl.py kaynaklar.txt
  python scihub_dl.py --doi 10.1016/j.tate.2019.05.003
  python scihub_dl.py kaynaklar.txt -o ./makaleler
  type kaynaklar.txt | python scihub_dl.py -
  python scihub_dl.py --list kaynaklar.txt
""",
    )
    parser.add_argument("input", nargs="?", help="DOI iceren dosya (- = stdin)")
    parser.add_argument("--doi", "-d", help="Tek DOI")
    parser.add_argument("--output", "-o", default="./downloads",
                        help="Indirme klasoru (varsayilan: ./downloads)")
    parser.add_argument("--delay", type=int, default=DELAY_SEC,
                        help=f"Istekler arasi bekleme saniye (varsayilan: {DELAY_SEC})")
    parser.add_argument("--mirror", "-m", default=SCIHUB_BASE,
                        help=f"Sci-Hub mirror (varsayilan: {SCIHUB_BASE})")
    parser.add_argument("--list", action="store_true",
                        help="Sadece DOI'leri listele, indirme")
    parser.add_argument("--no-scihub", action="store_true",
                        help="Sci-Hub'u atla, sadece yayinci sitesinden indir")

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
            sys.exit(f"Dosya bulunamadi: {args.input}")
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
                    print(f"Panodan {len(dois)} DOI algilandi.\n")
        except Exception:
            pass
        if not dois:
            parser.print_help()
            sys.exit(1)

    if not dois:
        sys.exit("Hic DOI bulunamadi.")

    # --- Sadece listele ---
    if args.list:
        for i, d in enumerate(dois, 1):
            print(f"  {i:2d}. {d}")
        print(f"\nToplam: {len(dois)} DOI")
        return

    # --- Hazirlik ---
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = args.mirror.rstrip("/")

    sep = "=" * 60
    print(sep)
    print(f"  Akademik PDF Toplu Indirici (Scrapling)")
    if args.no_scihub:
        print(f"  Mod    : Sadece yayinci (Sci-Hub atlanacak)")
    else:
        print(f"  Mirror : {base}")
        print(f"  Fallback: Yayinci sitesi (acik erisim)")
    print(f"  DOI    : {len(dois)} adet")
    print(f"  Klasor : {output_dir.resolve()}")
    print(f"  Bekleme: {args.delay}s")
    print(sep + "\n")

    for i, d in enumerate(dois, 1):
        print(f"  {i:2d}. {d}")
    print()

    # --- Indirme ---
    ok = 0
    fail: list[str] = []

    with FetcherSession(impersonate="chrome", stealthy_headers=True, timeout=30) as session:
        for i, doi in enumerate(dois, 1):
            success = False
            if args.no_scihub:
                success = download_one(doi, output_dir, session, i, len(dois),
                                       base, use_scihub=False)
            else:
                success = download_one(doi, output_dir, session, i, len(dois), base)
                if not success:
                    success = download_from_publisher(
                        doi, output_dir, session, i, len(dois))
            if success:
                ok += 1
            else:
                fail.append(doi)

            if i < len(dois):
                print(f"           ... {args.delay}s bekleniyor\n")
                time.sleep(args.delay)

    # --- Ozet ---
    print(f"\n{sep}")
    print(f"  OZET")
    print(f"  Basarili : {ok}/{len(dois)}")
    if fail:
        print(f"  Basarisiz: {len(fail)}")
        for d in fail:
            print(f"     - {d}")
    print(f"  Klasor   : {output_dir.resolve()}")
    print(sep)


if __name__ == "__main__":
    main()
