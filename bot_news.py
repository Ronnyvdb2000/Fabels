"""
bot_news.py — Dagelijkse actualiteitenbot voor granen, olie, kunstmest, oorlog/geopolitiek
en vee/pluimveeprijzen. Stuurt één Telegram-bericht per categorie naar een apart nieuwskanaal
(NEWS_TELEGRAM_CHAT_ID), gescheiden van de aandelen-/tradingbots. Geen CSV-logging.
"""

import os
import re
import time
import smtplib
import requests
import feedparser
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------- Configuratie ----------

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NEWS_CHAT_ID = os.environ["NEWS_TELEGRAM_CHAT_ID"]

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

MAX_ITEMS_PER_CATEGORIE = 5
NIEUWS_VENSTER_UUR = 30

FEGRA_URL = "https://fegra.be/home/agriculturalprices"
VIAVERDA_URL = "https://www.viaverda.be/Detail/category/marktberichten"
VDA_VARKENS_URL = "https://www.vda-ooigem.be/nl/marktprijzen/varkens"
VDA_EIEREN_URL = "https://www.vda-ooigem.be/nl/marktprijzen/eieren/eierprijzen-kruishoutem"
DEINZE_KIPPEN_URL = "https://www.deinze.be/kippenprijzen"

MAANDEN_NL = ["januari", "februari", "maart", "april", "mei", "juni",
              "juli", "augustus", "september", "oktober", "november", "december"]

CATEGORIEEN = {
    "🌾 Granen": {
        "query": "(tarwe OR mais OR sojabonen OR graanprijs OR wheat OR corn OR soybean OR Belgapomnotering OR aardappelprijs) markt prijs",
        "tickers": {"Tarwe (ZW=F)": "ZW=F", "Mais (ZC=F)": "ZC=F", "Soja (ZS=F)": "ZS=F"},
    },
    "🛢️ Olie": {
        "query": "(olieprijs OR crude oil OR OPEC OR brentolie OR WTI) markt",
        "tickers": {"WTI (CL=F)": "CL=F", "Brent (BZ=F)": "BZ=F", "Aardgas (NG=F)": "NG=F"},
    },
    "🧪 Kunstmest": {
        "query": "(kunstmest OR fertilizer OR ureum OR urea OR potash OR fosfaat) prijs markt",
        "tickers": {},
    },
    "⚔️ Oorlog & geopolitiek": {
        "query": "(oorlog OR geopolitiek OR conflict OR sancties) (grondstoffen OR olie OR graan OR energie)",
        "tickers": {},
    },
    "🐖 Vee & Pluimvee": {
        "query": "(varkensprijs OR biggenprijs OR eierprijs OR pluimveeprijs OR vleesvarkens) markt",
        "tickers": {},
    },
}

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=nl&gl=BE&ceid=BE:nl"


# ---------- Nieuws ophalen ----------

def haal_nieuws_op(query, max_items=MAX_ITEMS_PER_CATEGORIE):
    url = GOOGLE_NEWS_RSS.format(query=requests.utils.quote(query))
    feed = feedparser.parse(url)

    grens = datetime.now(timezone.utc) - timedelta(hours=NIEUWS_VENSTER_UUR)
    artikels = []

    for entry in feed.entries:
        try:
            gepubliceerd = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except (AttributeError, TypeError):
            gepubliceerd = None

        if gepubliceerd and gepubliceerd < grens:
            continue

        bron = entry.get("source", {}).get("title", "") if hasattr(entry, "source") else ""
        artikels.append({
            "titel": entry.title,
            "link": entry.link,
            "bron": bron,
            "gepubliceerd": gepubliceerd,
        })

        if len(artikels) >= max_items:
            break

    return artikels


# ---------- Futuresprijzen (yfinance) ----------

def haal_futures_prijzen_op(tickers: dict):
    resultaten = {}
    for label, ticker in tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) < 2:
                continue
            laatste = hist["Close"].iloc[-1]
            vorige = hist["Close"].iloc[-2]
            verandering_pct = (laatste - vorige) / vorige * 100
            resultaten[label] = (laatste, verandering_pct)
        except Exception as e:
            print(f"Kon prijs niet ophalen voor {ticker}: {e}")
    return resultaten


# ---------- Fegra tarweprijs ----------

def haal_fegra_tarweprijs_op():
    """Scrapt de indicatieve tarweprijs (STANDAARD TARWE) van Fegra/Synagra."""
    try:
        resp = requests.get(FEGRA_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tabel = soup.find("table")
        if not tabel:
            print("Fegra: geen <table> gevonden op de pagina.")
            return {}

        resultaten = {}
        rijen = tabel.find_all("tr")
        for rij in rijen[1:]:
            cellen = rij.find_all("td")
            if len(cellen) < 3:
                continue
            label = cellen[0].get_text(strip=True)
            if "TARWE" not in label.upper():
                continue
            try:
                laatste = float(cellen[1].get_text(strip=True).replace(",", "."))
                vorige = float(cellen[2].get_text(strip=True).replace(",", "."))
                verandering = laatste - vorige
                resultaten[label] = (laatste, verandering)
            except (ValueError, IndexError):
                continue

        return resultaten
    except Exception as e:
        print(f"Fegra-scrape mislukt: {e}")
        return {}


# ---------- Aardappelprijzen (Viaverda — herpubliceert ook Belgapomnotering) ----------

def haal_aardappelprijs_op():
    """Scrapt het meest recente Viaverda-bericht (bevat ook Belgapomnotering-cijfers als tekst)."""
    try:
        resp = requests.get(VIAVERDA_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tekst = soup.get_text("\n")

        maanden_patroon = "|".join(MAANDEN_NL)
        patroon = re.compile(
            r"(\d{1,2}\s+(?:" + maanden_patroon + r")\s+\d{4})\s*-\s*(.+?)"
            r"(?=\n\d{1,2}\s+(?:" + maanden_patroon + r")\s+\d{4}\s*-|\Z)",
            re.DOTALL,
        )
        match = patroon.search(tekst)
        if not match:
            print("Aardappelprijzen (Viaverda): geen berichtblok gevonden.")
            return {}

        datum_str, inhoud = match.groups()
        inhoud = " ".join(inhoud.split())[:400]
        return {"datum": datum_str.strip(), "tekst": inhoud}
    except Exception as e:
        print(f"Aardappelprijzen-scrape (Viaverda) mislukt: {e}")
        return {}


# ---------- Varkens- en biggenprijzen (Vanden Avenne Ooigem) ----------

def haal_varkensprijzen_op():
    """Scrapt de meest recente week uit de VDA-varkenstabel."""
    try:
        resp = requests.get(VDA_VARKENS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        tabellen = pd.read_html(resp.text)
        if not tabellen:
            return {}
        df = tabellen[0]
        laatste_rij = df.iloc[0]
        kolommen = df.columns.tolist()

        resultaten = {"datum": str(laatste_rij[kolommen[1]])}
        for kol in kolommen[2:]:
            resultaten[str(kol)] = laatste_rij[kol]
        return resultaten
    except Exception as e:
        print(f"Varkensprijzen-scrape mislukt: {e}")
        return {}


# ---------- Eierprijzen (Vanden Avenne Ooigem, Kruishoutem) ----------

def haal_eierprijzen_op():
    """Scrapt de meest recente week uit de bruinschalig-verrijkte-kooi tabel (eerste tabel op de pagina)."""
    try:
        resp = requests.get(VDA_EIEREN_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        tabellen = pd.read_html(resp.text)
        if not tabellen:
            return {}
        df = tabellen[0]
        laatste_rij = df.iloc[0]
        kolommen = df.columns.tolist()

        gewichtsklasse = kolommen[3] if len(kolommen) > 3 else kolommen[-1]
        return {
            "datum": str(laatste_rij[kolommen[1]]),
            "gewichtsklasse": str(gewichtsklasse),
            "prijs": laatste_rij[gewichtsklasse],
        }
    except Exception as e:
        print(f"Eierprijzen-scrape mislukt: {e}")
        return {}


# ---------- Slachtpluimveeprijzen (Stad Deinze) ----------

def haal_kippenprijzen_op():
    """Scrapt de meest recente prijzencommissie-tabel van Stad Deinze."""
    try:
        resp = requests.get(DEINZE_KIPPEN_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        tabellen = pd.read_html(resp.text)
        if not tabellen:
            return {}
        df = tabellen[0]
        resultaten = {}
        for _, rij in df.iterrows():
            resultaten[str(rij.iloc[0])] = str(rij.iloc[1])
        return resultaten
    except Exception as e:
        print(f"Kippenprijzen-scrape mislukt: {e}")
        return {}


# ---------- Berichten opbouwen ----------

def bouw_categorie_bericht(naam, artikels, prijzen, extra_secties=None):
    """extra_secties: lijst van (titel, [regels]) tuples, elk als eigen blok toegevoegd."""
    regels = [f"<b>{naam}</b>", ""]

    if prijzen:
        for label, (koers, pct) in prijzen.items():
            pijl = "🔺" if pct >= 0 else "🔻"
            regels.append(f"{label}: {koers:.2f} ({pijl} {pct:+.2f}%)")
        regels.append("")

    if extra_secties:
        for titel, sectie_regels in extra_secties:
            if not sectie_regels:
                continue
            regels.append(f"<b>{titel}</b>")
            regels.extend(sectie_regels)
            regels.append("")

    if artikels:
        for a in artikels:
            bron_str = f" — {a['bron']}" if a["bron"] else ""
            regels.append(f"• <a href='{a['link']}'>{a['titel']}</a>{bron_str}")
    else:
        regels.append("Geen recent nieuws gevonden binnen het tijdsvenster.")

    return "\n".join(regels)


# ---------- Telegram ----------

def stuur_telegram(tekst, max_pogingen=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": NEWS_CHAT_ID,
        "text": tekst,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    for poging in range(max_pogingen):
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            wacht = resp.json().get("parameters", {}).get("retry_after", 5)
            print(f"Telegram rate limit, wacht {wacht}s...")
            time.sleep(wacht)
            continue
        print(f"Telegram-fout ({resp.status_code}): {resp.text}")
        return False

    return False


# ---------- E-mail ----------

def stuur_email_samenvatting(categorie_berichten: dict):
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_RECEIVER):
        print("E-mail secrets ontbreken, e-mail wordt overgeslagen.")
        return

    vandaag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Dagelijkse actua: granen / olie / kunstmest / oorlog / vee — {vandaag}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_RECEIVER

    html_delen = [bericht.replace("\n", "<br>") for bericht in categorie_berichten.values()]
    html = "<hr>".join(html_delen)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)


# ---------- Main ----------

def main():
    categorie_berichten = {}

    for naam, config in CATEGORIEEN.items():
        artikels = haal_nieuws_op(config["query"])
        prijzen = haal_futures_prijzen_op(config["tickers"])
        extra_secties = []

        if naam == "🌾 Granen":
            fegra_prijzen = haal_fegra_tarweprijs_op()
            print(f"Fegra-resultaat: {fegra_prijzen if fegra_prijzen else 'LEEG/MISLUKT'}")
            if fegra_prijzen:
                regels = [
                    f"{label}: {laatste:.1f} ({'🔺' if verandering >= 0 else '🔻'} {verandering:+.1f})"
                    for label, (laatste, verandering) in fegra_prijzen.items()
                ]
                extra_secties.append(("Fegra tarwe (BE, €/ton)", regels))

            aardappel = haal_aardappelprijs_op()
            print(f"Aardappelprijzen-resultaat: {aardappel if aardappel else 'LEEG/MISLUKT'}")
            if aardappel:
                titel = f"Aardappelen (Viaverda/Belgapom, {aardappel.get('datum', '?')})"
                extra_secties.append((titel, [aardappel.get("tekst", "")]))

        if naam == "🐖 Vee & Pluimvee":
            varkens = haal_varkensprijzen_op()
            print(f"Varkensprijzen-resultaat: {varkens if varkens else 'LEEG/MISLUKT'}")
            if varkens:
                regels = [f"{k}: {v}" for k, v in varkens.items() if k != "datum"]
                titel = f"Varkens/Biggen (VDA, week van {varkens.get('datum', '?')})"
                extra_secties.append((titel, regels))

            eieren = haal_eierprijzen_op()
            print(f"Eierprijzen-resultaat: {eieren if eieren else 'LEEG/MISLUKT'}")
            if eieren:
                titel = f"Eieren verrijkte kooi (VDA, week van {eieren.get('datum', '?')})"
                regel = [f"Klasse {eieren.get('gewichtsklasse', '?')}g: {eieren.get('prijs', '?')} €/100 st."]
                extra_secties.append((titel, regel))

            kippen = haal_kippenprijzen_op()
            print(f"Kippenprijzen-resultaat: {kippen if kippen else 'LEEG/MISLUKT'}")
            if kippen:
                regels = [f"{k}: {v}" for k, v in kippen.items()]
                extra_secties.append(("Slachtpluimvee (Deinze)", regels))

        bericht = bouw_categorie_bericht(naam, artikels, prijzen, extra_secties)
        categorie_berichten[naam] = bericht

        verzonden = stuur_telegram(bericht)
        print(f"{naam}: {'verzonden' if verzonden else 'MISLUKT'} ({len(artikels)} artikels)")

        time.sleep(1)

    stuur_email_samenvatting(categorie_berichten)


if __name__ == "__main__":
    main()
