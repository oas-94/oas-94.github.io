"""Henter tall fra de åpne plattformene (Bluesky, Mastodon, Google Scholar).

Kjøres hver time av .github/workflows/tall.yml. Bare standardbiblioteket.
Skriver til tall/data/:
  sky.csv              tid,plattform,maal,verdi,kilde  (legges til, aldri skrevet om)
  innlegg_sky.json     siste tall for hvert innlegg på Bluesky og Mastodon
  folgere_sky.json     hvem som følger, med første og siste gang de ble sett
  scholar_sky.json     siteringer per år og per publikasjon
Raden legges bare til når et tall har endret seg, eller når forrige måling
er over seks timer gammel, slik at repoet ikke får en endring hver time.
"""
import csv, json, os, re, sys, time, urllib.request, urllib.error, html
from datetime import datetime, timezone

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "tall", "data")
UA = {"User-Agent": "orjansamuelsen.no-tall/1.0 (personlig statistikk)"}
BSKY = "did:plc:vqwoze725tzjgh2fycsc3tbr"
MASTO_INST, MASTO_ACCT = "mastodon.social", "orjansamuelsen"
SCHOLAR = "1qEn9ZQAAAAJ"
HJERTESLAG = 6 * 3600

NAA = datetime.now(timezone.utc).replace(microsecond=0)
NAA_S = NAA.isoformat().replace("+00:00", "Z")


def apne(url, headers=None, forsok=3):
    """Åpner en adresse, med to nye forsøk ved nett- eller serverfeil."""
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for i in range(forsok):
        try:
            return urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == forsok - 1:
                raise
        except urllib.error.URLError:
            if i == forsok - 1:
                raise
        time.sleep(5 * (i + 1))


def hent(url, headers=None, raa=False):
    with apne(url, headers) as r:
        b = r.read()
        tegnsett = r.headers.get_content_charset() or "utf-8"
    return b.decode(tegnsett, "replace") if raa else json.loads(b)


def les_json(navn, standard):
    p = os.path.join(DATA, navn)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return standard


def skriv_json(navn, obj):
    p = os.path.join(DATA, navn)
    ny = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)
    gammel = open(p, encoding="utf-8").read() if os.path.exists(p) else None
    if ny != gammel:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(ny)
        return True
    return False


# ---------------- Bluesky ----------------
def bluesky():
    api = "https://public.api.bsky.app/xrpc/"
    p = hent(api + "app.bsky.actor.getProfile?actor=" + BSKY)
    folgere, cur = [], None
    while True:
        d = hent(api + f"app.bsky.graph.getFollowers?actor={BSKY}&limit=100" + (f"&cursor={cur}" if cur else ""))
        folgere += [(f["did"], f.get("handle", "")) for f in d["followers"]]
        cur = d.get("cursor")
        if not cur or not d["followers"]:
            break
    innlegg, cur = [], None
    while True:
        d = hent(api + f"app.bsky.feed.getAuthorFeed?actor={BSKY}&limit=100&filter=posts_with_replies" + (f"&cursor={cur}" if cur else ""))
        for it in d["feed"]:
            post = it["post"]
            if post["author"]["did"] != BSKY or it.get("reason"):
                continue  # gjenpostinger av andre teller ikke som egne innlegg
            rkey = post["uri"].rsplit("/", 1)[-1]
            rec = post.get("record", {})
            innlegg.append({
                "id": post["uri"], "url": f"https://bsky.app/profile/orjansamuelsen.no/post/{rkey}",
                "tid": rec.get("createdAt", post.get("indexedAt")),
                "tekst": rec.get("text", "")[:280],
                "svar": bool(rec.get("reply")),
                "likes": post.get("likeCount", 0), "delinger": post.get("repostCount", 0),
                "kommentarer": post.get("replyCount", 0), "sitater": post.get("quoteCount", 0),
            })
        cur = d.get("cursor")
        if not cur or not d["feed"]:
            break
    maal = {
        "folgere": p["followersCount"],          # tallet profilen viser
        "folgere_aktive": len(folgere),          # bare kontoer som fortsatt finnes
        "folger": p["followsCount"],
        "innlegg": p["postsCount"],
    }
    return maal, innlegg, folgere


# ---------------- Mastodon ----------------
def mastodon():
    api = f"https://{MASTO_INST}/api/v1/"
    a = hent(api + "accounts/lookup?acct=" + MASTO_ACCT)
    innlegg, url = [], api + f"accounts/{a['id']}/statuses?limit=40&exclude_reblogs=true"
    while url:
        with apne(url) as r:
            d = json.loads(r.read())
            link = r.headers.get("Link", "")
        for s in d:
            tekst = html.unescape(re.sub(r"<[^>]+>", " ", s.get("content", ""))).strip()
            innlegg.append({
                "id": s["id"], "url": s["url"], "tid": s["created_at"], "tekst": re.sub(r"\s+", " ", tekst)[:280],
                "svar": bool(s.get("in_reply_to_id")),
                "likes": s["favourites_count"], "delinger": s["reblogs_count"], "kommentarer": s["replies_count"],
            })
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if (m and d) else None
    folgere, url = [], api + f"accounts/{a['id']}/followers?limit=80"
    while url:
        with apne(url) as r:
            d = json.loads(r.read())
            link = r.headers.get("Link", "")
        folgere += [(f["id"], f["acct"]) for f in d]
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if (m and d) else None
    maal = {"folgere": a["followers_count"], "folger": a["following_count"], "innlegg": a["statuses_count"]}
    return maal, innlegg, folgere


# ---------------- Google Scholar ----------------
def les_scholar(side):
    """Leser profilsiden. Brukes også av utvidelsen (samme regler i JS)."""
    tall = [int(x) for x in re.findall(r'class="gsc_rsb_std">(\d+)<', side)]
    if len(tall) < 6:
        raise ValueError("fant ikke sitattabellen")
    aar = [int(x) for x in re.findall(r'class="gsc_g_t"[^>]*>(\d{4})<', side)]
    # Søylene står med z-index som sier hvilket år de hører til (år uten siteringer mangler)
    stolper = re.findall(r'<a[^>]*class="gsc_g_a"[^>]*style="[^"]*z-index:(\d+)[^"]*"[^>]*><span class="gsc_g_al">(\d+)</span>', side)
    per_aar = {str(a): 0 for a in aar}
    for z, v in stolper:
        i = len(aar) - int(z)
        if 0 <= i < len(aar):
            per_aar[str(aar[i])] = int(v)
    pub = []
    for rad in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', side, re.S):
        t = re.search(r'class="gsc_a_at"[^>]*>(.*?)</a>', rad, re.S)
        c = re.search(r'class="gsc_a_ac[^"]*"[^>]*>(\d*)<', rad)
        y = re.search(r'class="gsc_a_h gsc_a_hc[^"]*">(\d*)<', rad)
        if t:
            pub.append({"tittel": html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip(),
                        "siteringer": int(c.group(1)) if c and c.group(1) else 0,
                        "aar": int(y.group(1)) if y and y.group(1) else None})
    return {"siteringer": tall[0], "siteringer_5aar": tall[1], "h_indeks": tall[2],
            "h_indeks_5aar": tall[3], "i10_indeks": tall[4], "i10_indeks_5aar": tall[5],
            "per_aar": per_aar, "publikasjoner": pub}


def scholar():
    side = hent(f"https://scholar.google.com/citations?user={SCHOLAR}&hl=en&cstart=0&pagesize=100", raa=True)
    s = les_scholar(side)
    maal = {k: s[k] for k in ("siteringer", "h_indeks", "i10_indeks")}
    maal["publikasjoner"] = len(s["publikasjoner"])
    return maal, s


# ---------------- Lagring ----------------
def siste_rader(sti):
    siste = {}
    if os.path.exists(sti):
        with open(sti, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                siste[(r["plattform"], r["maal"])] = r
    return siste


def oppdater_folgere(alle, plattform, folgere):
    """Holder rede på når hver følger først ble sett og når de forsvant, så nye og tapte følgere kan telles."""
    reg = alle.setdefault(plattform, {})
    naa = {i for i, _ in folgere}
    for i, navn in folgere:
        r = reg.setdefault(i, {"forst": NAA_S, "navn": navn})
        r["navn"] = navn
        r.pop("borte", None)
    for i, r in reg.items():
        if i not in naa and "borte" not in r:
            r["borte"] = NAA_S


def main():
    os.makedirs(DATA, exist_ok=True)
    sti = os.path.join(DATA, "sky.csv")
    siste = siste_rader(sti)
    rader, feil = [], []
    innlegg = les_json("innlegg_sky.json", {})
    folg = les_json("folgere_sky.json", {})
    for navn, fn in (("bluesky", bluesky), ("mastodon", mastodon)):
        try:
            maal, inn, f = fn()
            innlegg[navn] = sorted(inn, key=lambda x: x["tid"], reverse=True)
            oppdater_folgere(folg, navn, f)
            for k, v in maal.items():
                rader.append([NAA_S, navn, k, v, "api"])
        except Exception as e:
            feil.append(f"{navn}: {e}")
    if os.environ.get("TALL_SCHOLAR", "1") == "1":
        try:
            maal, s = scholar()
            s["tid"] = NAA_S
            skriv_json("scholar_sky.json", s)
            for k, v in maal.items():
                rader.append([NAA_S, "scholar", k, v, "side"])
        except Exception as e:
            feil.append(f"scholar: {e}")

    endret = False
    eldst = 0
    for r in rader:
        s = siste.get((r[1], r[2]))
        if not s or str(s["verdi"]) != str(r[3]):
            endret = True
        else:
            alder = (NAA - datetime.fromisoformat(s["tid"].replace("Z", "+00:00"))).total_seconds()
            eldst = max(eldst, alder)
    if rader and (endret or eldst >= HJERTESLAG):
        ny = not os.path.exists(sti)
        with open(sti, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            if ny:
                w.writerow(["tid", "plattform", "maal", "verdi", "kilde"])
            w.writerows(rader)
    skriv_json("innlegg_sky.json", innlegg)
    skriv_json("folgere_sky.json", folg)
    # Status skrives bare når feillisten endrer seg, så den ikke gir en endring hver time
    status = les_json("status_sky.json", {})
    if status.get("feil") != feil:
        status = {"feil": feil, "siden": NAA_S}
        skriv_json("status_sky.json", status)
    for f in feil:
        print("FEIL", f, file=sys.stderr)
    print(f"{len(rader)} tall, endret={endret}, feil={len(feil)}")


if __name__ == "__main__":
    main()
