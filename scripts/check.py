# Checker used for the Veilvex 2026 small business websites study.
# Input: sites.tsv with columns city, niche, url (the list itself is not published). Output: results.json.
import csv, re, time, json, sys, concurrent.futures as cf
import requests
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36", "Accept-Language": "en,ru;q=0.8,de;q=0.6"}
AI_BOTS = ["gptbot", "oai-searchbot", "chatgpt-user", "claudebot", "perplexitybot", "google-extended", "ccbot", "anthropic-ai"]
PRICE_WORDS = re.compile(r"(pricing|prices|price list|tarif|preise|preisliste|precios|tarifas|prezzi|listino|cennik|ceny|fiyat|цены|прайс|стоимость|price)", re.I)
MONEY = re.compile(r"((€|\$|£|₽|₸|zł|AED|S\$|R\$)\s?\d|\d[\d\s.,]*\s?(€|\$|£|₽|руб|тг|₸|zł|AED|dhs|eur|usd|TL|₺|грн|Kč|lari|₾))", re.I)
def robots(base):
    try:
        r = requests.get(base.rstrip('/') + '/robots.txt', headers=UA, timeout=15)
        if r.status_code != 200 or 'html' in r.headers.get('content-type', ''): return []
        blocked, agents, lines = set(), [], r.text.lower().splitlines()
        cur, prev_ua = [], False
        for ln in lines:
            ln = ln.split('#')[0].strip()
            if not ln: continue
            if ln.startswith('user-agent:'):
                if not prev_ua: cur = []
                cur.append(ln.split(':', 1)[1].strip()); prev_ua = True
            else:
                prev_ua = False
                if ln.startswith('disallow:') and ln.split(':', 1)[1].strip() == '/':
                    for a in cur:
                        if a == '*' or any(b == a for b in AI_BOTS): blocked.add(a)
        return sorted(blocked)
    except Exception: return None
def check(row):
    city, niche, url = row
    d = {"city": city, "niche": niche, "url": url}
    try:
        t = time.time()
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        d["secs"] = round(time.time() - t, 2)
        d["status"] = r.status_code
        d["final"] = r.url
        h = r.text
        hl = h.lower()
        d["kb"] = round(len(r.content) / 1024)
        d["https"] = r.url.startswith("https://")
        d["viewport"] = bool(re.search(r'<meta[^>]+name=["\']?viewport', hl))
        m = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
        d["title"] = bool(m and m.group(1).strip())
        d["desc"] = bool(re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'][^"\']{20,}', hl) or re.search(r'<meta[^>]+content=["\'][^"\']{20,}["\'][^>]+name=["\']description', hl))
        d["tel"] = "href=\"tel:" in hl or "href='tel:" in hl or "href=tel:" in hl
        d["whatsapp"] = bool(re.search(r"wa\.me/|api\.whatsapp\.com|whatsapp://|web\.whatsapp|wa\.link", hl))
        d["telegram"] = "t.me/" in hl
        d["form"] = "<form" in hl
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        d["money"] = bool(MONEY.search(text))
        links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', h, re.S | re.I)
        d["pricelink"] = any(PRICE_WORDS.search(href + " " + re.sub("<[^>]+>", "", txt)) for href, txt in links)
        d["jsonld"] = "application/ld+json" in hl
        d["localbiz"] = bool(re.search(r'"@type"\s*:\s*\[?\s*"(localbusiness|dentist|medicalclinic|medicalbusiness|plumber|electrician|roofingcontractor|autorepair|hairsalon|beautysalon|dayspa|legalservice|attorney|accountingservice|realestateagent|restaurant|veterinarycare|healthclub|exercisegym|movingcompany|professionalservice|homeandconstructionbusiness|organization|physician|educationalorganization)', hl))
        d["llms"] = None
    except Exception as e:
        d["error"] = type(e).__name__
    d["ai_blocked"] = robots(url)
    return d
rows = list(csv.reader(open("sites.tsv"), delimiter="\t"))
with cf.ThreadPoolExecutor(16) as ex:
    res = list(ex.map(check, rows))
json.dump(res, open("results.json", "w"), indent=1, ensure_ascii=False)
print(len(res), sum(1 for r in res if r.get("status") == 200))
