"""
bot_news.py — Dagelijkse actualiteitenbot voor granen, olie, kunstmest en oorlog/geopolitiek.
Stuurt één Telegram-bericht per categorie naar een apart nieuwskanaal (NEWS_TELEGRAM_CHAT_ID),
gescheiden van de aandelen-/tradingbots. Geen CSV-logging.
Granen-categorie bevat ook de Belgische Fegra-tarweprijs (Synagra-notering).
"""

import os
import time
import smtplib
import requests
import feedparser
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------- Configuratie ----------

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NEWS_CHAT_ID = os.environ["NEWS_TELEGRAM_CHAT_ID"]  # apart kanaal, los van de tradingbots

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

MAX_ITEMS_PER_CATEGORIE = 5
NIEUWS_VENSTER_UUR = 30  # alleen artikels van de laatste 30 uur

FEGRA_URL = "https://fegra.be/home/agriculturalprices"

CATEGORIEEN = {
    "🌾 Granen": {
        "query": "(tarwe OR mais OR sojabonen OR graanprijs OR wheat OR corn OR soybean) markt prijs",
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
}

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=nl&gl=BE&ceid=BE:nl"


# ---------- Nieuws ophalen ----------

def haal_nieuws_op(query, max_items=MAX_ITEMS_PER_CATEGORIE):
    """Haalt recente nieuwsartikels op via Google News RSS (gratis, geen API-key nodig)."""
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
    """Geeft laatste slotkoers + %-verandering t.o.v. vorige sessie per ticker terug."""
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


# ---------- Fegra tarweprijs (Belgische markt) ----------

def haal_fegra_tarweprijs_op():
    """Scrapt de indicatieve tarweprijs (STANDAARD TARWE) van Fegra/Synagra."""
    try:
        resp = requests.get(FEGRA_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tabel = soup.find("table")
        if not tabel:
            return {}

        resultaten = {}
        rijen = tabel.find_all("tr")
        for rij in rijen[1:]:  # eerste rij = kolomkoppen (datums)
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


# ---------- Berichten opbouwen ----------

def bouw_categorie_bericht(naam, artikels, prijzen, fegra_prijzen=None):
    regels = [f"<b>{naam}</b>", ""]

    if prijzen:
        for label, (koers, pct) in prijzen.items():
            pijl = "🔺" if pct >= 0 else "🔻"
            regels.append(f"{label}: {koers:.2f} ({pijl} {pct:+.2f}%)")
        regels.append("")

    if fegra_prijzen:
        regels.append("<b>Fegra (BE, €/ton):</b>")
        for label, (laatste, verandering) in fegra_prijzen.items():
            pijl = "🔺" if verandering >= 0 else "🔻"
            regels.append(f"{label}: {laatste:.1f} ({pijl} {verandering:+.1f})")
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
    msg["Subject"] = f"Dagelijkse actua: granen / olie / kunstmest / oorlog — {vandaag}"
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
        fegra_prijzen = haal_fegra_tarweprijs_op() if naam == "🌾 Granen" else None

        bericht = bouw_categorie_bericht(naam, artikels, prijzen, fegra_prijzen)
        categorie_berichten[naam] = bericht

        verzonden = stuur_telegram(bericht)
        print(f"{naam}: {'verzonden' if verzonden else 'MISLUKT'} ({len(artikels)} artikels)")

        time.sleep(1)  # kleine pauze tussen berichten

    stuur_email_samenvatting(categorie_berichten)


if __name__ == "__main__":
    main()
