#!/usr/bin/env python3
"""
Buduje feed.xml dla Meble.pl na podstawie feedu FeedFix (entafix).

Zasada: pobierz feed na bieżąco, sprawdź czy nie jest uszkodzony, dopiero
wtedy nadpisz feed.xml. Jeśli cokolwiek jest nie tak - nie dotykaj pliku,
bo lepiej mieć stare dane niż puste.

Bloki <produkt> są przepisywane bajt w bajt z oryginału (zachowane CDATA,
kolejność i formatowanie). Filtrowanie polega wyłącznie na pomijaniu
całych bloków - nic nie jest przepisywane ani normalizowane.

Tryb publikacji sterowany zmienną środowiskową FEED_MODE:
  categorized  (domyślny) - tylko pozycje z wypełnioną <sciezka>.
                Bezpieczne: Meble.pl odrzuca pozycje bez kategorii.
  all          - cały katalog. Włączyć DOPIERO po uzupełnieniu
                mapowania kategorii w panelu FeedFix.
"""

import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCE_URL = os.environ.get("FEED_SOURCE_URL", "").strip()
MODE = os.environ.get("FEED_MODE", "categorized").strip().lower()
OUT = Path(os.environ.get("FEED_OUT", "feed.xml"))

# Progi bezpieczeństwa - poniżej nich uznajemy pobranie za nieudane.
MIN_BYTES = 200_000
MIN_ITEMS = 150

# Pozycje, których nigdy nie publikujemy (np. warianty usunięte ze sklepu,
# a wciąż obecne w feedzie źródłowym). Lista po ID wariantu Shopify.
BLOCKLIST = {
    # 2026-10-10: warianty nieistniejące już w Shopify, a sprzedawane
    # na Meble.pl z lutowego pliku. Zostawione jako zabezpieczenie na
    # wypadek, gdyby wróciły do feedu źródłowego.
    "55932183904638",  # Pufa Sakwa L Amore Plusz - A38
    "55932184002942",  # Pufa Sakwa L Amore Plusz - A45
    "56836858347902",  # Fotel Simone Sztruks - SZ93
    "55932195078526",  # Worek Sako Amore Plusz - A9
    "55932195242366",  # Worek Sako Amore Plusz - A38
    "55932195307902",  # Worek Sako Amore Plusz - A44
    "55932195340670",  # Worek Sako Amore Plusz - A45
    "56836860281214",  # Pufa Kostka Sztruks - SZ93
    "55932385165694",  # Pufa Kostka Amore Plusz - A9
    "55932385329534",  # Pufa Kostka Amore Plusz - A38
    "55932385362302",  # Pufa Kostka Amore Plusz - A44
    "55932385395070",  # Pufa Kostka Amore Plusz - A45
}

ITEM_RE = re.compile(rb"[ \t]*<produkt>.*?</produkt>\s*?\n?", re.DOTALL)

# --- Konwersja kosztów wysyłki -------------------------------------------
#
# FeedFix generuje:
#     <koszt_1>35.0</koszt_1> ... <koszt_4>35.0</koszt_4>
#
# Schemat Meble.pl tego nie zna - walidator zwraca dla każdej pozycji
# "Element 'koszt_1': This element is not expected." i odrzuca cały plik.
# Poprawna postać (zweryfikowana: plik z lutego przechodzi walidację):
#     <koszty_transportu><koszt wysylka="Kurier" platnosc="przelew"
#       koszt-kolejna-sztuka="0" ilosc-w-paczce="1">35.0</koszt>...</koszty_transportu>
#
# Mapowanie numeru na formę płatności odtworzone z pliku lutowego.
KOSZT_RE = re.compile(rb"[ \t]*<koszt_([1-4])>([^<]*)</koszt_\1>[ \t]*\r?\n?")
PLATNOSC = {b"1": b"przelew", b"2": b"pobranie", b"3": b"e-platnosc", b"4": b"raty"}
MAX_W_PACZCE_RE = re.compile(
    rb'<cecha nazwa="Maksymalna ilo\xc5\x9b\xc4\x87 w przesy\xc5\x82ce:">\s*'
    rb"<definicja>(?:<!\[CDATA\[)?\s*(\d+)",
    re.DOTALL,
)


def convert_shipping(block: bytes) -> tuple[bytes, bool]:
    """Zamienia <koszt_1..4> na <koszty_transportu>. Zwraca (blok, czy_zmieniono)."""
    matches = list(KOSZT_RE.finditer(block))
    if not matches:
        return block, False

    per_package = b"1"
    found = MAX_W_PACZCE_RE.search(block)
    if found and found.group(1) != b"0":
        per_package = found.group(1)

    parts = []
    for match in matches:
        platnosc = PLATNOSC[match.group(1)]
        value = match.group(2).strip()
        parts.append(
            b'<koszt wysylka="Kurier" platnosc="' + platnosc + b'" '
            b'koszt-kolejna-sztuka="0" ilosc-w-paczce="' + per_package + b'">'
            + value
            + b"</koszt>"
        )

    indent = block[: matches[0].start()].rsplit(b"\n", 1)[-1]
    if indent.strip():
        indent = b"      "
    replacement = (
        indent + b"<koszty_transportu>" + b"".join(parts) + b"</koszty_transportu>\n"
    )
    return block[: matches[0].start()] + replacement + block[matches[-1].end() :], True


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines)
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def strip_timestamp(blob: bytes) -> bytes:
    """Usuwa <data_utworzenia>, żeby porównywać samą treść."""
    start = blob.find(b"<data_utworzenia>")
    if start == -1:
        return blob
    end = blob.find(b"</data_utworzenia>", start)
    if end == -1:
        return blob
    return blob[:start] + blob[end:]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pufypl-feed-mirror/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            status = getattr(resp, "status", None)
            if status is not None and status != 200:
                fail(f"HTTP {status} przy pobieraniu feedu źródłowego.")
            return resp.read()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(f"Nie udało się pobrać feedu źródłowego: {exc}")


def validate(raw: bytes) -> tuple[int, str]:
    """Sprawdza strukturę całego pliku. Zwraca (liczba pozycji, data)."""
    if len(raw) < MIN_BYTES:
        fail(f"Pobrany plik ma {len(raw)} B, próg to {MIN_BYTES} B. Przerywam.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        fail(f"Pobrany plik nie jest poprawnym XML: {exc}")
    if root.tag != "oferta":
        fail(f"Nieoczekiwany element główny: <{root.tag}>, oczekiwano <oferta>.")
    container = root.find("produkty")
    if container is None:
        fail("Brak elementu <produkty> w feedzie źródłowym.")
    total = len(container.findall("produkt"))
    if total < MIN_ITEMS:
        fail(f"Feed źródłowy ma {total} pozycji, próg to {MIN_ITEMS}. Przerywam.")
    return total, (root.findtext("data_utworzenia") or "?").strip()


def keep(block: bytes) -> tuple[bool, str]:
    """Decyduje o jednym bloku <produkt>. Zwraca (zostawić, powód odrzucenia)."""
    try:
        node = ET.fromstring(block.strip())
    except ET.ParseError:
        return False, "broken"

    item_id = (node.findtext("id") or "").strip()
    name = (node.findtext("nazwa") or "").strip()
    price = (node.findtext("cena_brutto") or "").strip()
    category = (node.findtext("sciezka") or "").strip()

    if item_id in BLOCKLIST:
        return False, "blocked"

    try:
        price_ok = float(price.replace(",", ".")) > 0
    except ValueError:
        price_ok = False
    if not item_id or not name or not price_ok:
        return False, "broken"

    if MODE == "categorized" and not category:
        return False, "category"

    return True, ""


def main():
    if not SOURCE_URL:
        fail("Brak FEED_SOURCE_URL. Ustaw sekret FEED_SOURCE_URL w repozytorium.")
    if MODE not in {"categorized", "all"}:
        fail(f"Nieznany FEED_MODE: {MODE!r}. Dozwolone: categorized, all.")

    raw = fetch(SOURCE_URL)
    total, created = validate(raw)

    matches = list(ITEM_RE.finditer(raw))
    if len(matches) != total:
        fail(
            f"Niespójny odczyt: XML ma {total} pozycji, a dopasowano "
            f"{len(matches)} bloków <produkt>. Przerywam, żeby nie uciąć feedu."
        )

    prefix = raw[: matches[0].start()]
    suffix = raw[matches[-1].end() :]

    kept = []
    dropped = {"category": 0, "blocked": 0, "broken": 0}
    converted = 0
    for match in matches:
        block = match.group(0)
        ok, reason = keep(block)
        if not ok:
            dropped[reason] += 1
            continue
        block, changed = convert_shipping(block)
        converted += changed
        kept.append(block)

    if len(kept) < MIN_ITEMS:
        fail(
            f"Po filtrowaniu zostało {len(kept)} pozycji, próg to {MIN_ITEMS}. "
            "Nie nadpisuję feed.xml."
        )

    new_blob = prefix + b"".join(kept) + suffix

    # Druga walidacja - plik wychodzący musi się parsować i mieć tyle pozycji,
    # ile zostawiliśmy. Zabezpieczenie przed błędem w cięciu bloków.
    try:
        out_root = ET.fromstring(new_blob)
    except ET.ParseError as exc:
        fail(f"Plik wynikowy nie parsuje się jako XML: {exc}")
    out_count = len(out_root.findall(".//produkt"))
    if out_count != len(kept):
        fail(f"Plik wynikowy ma {out_count} pozycji, oczekiwano {len(kept)}.")

    # Żaden <koszt_N> nie może zostać - walidator Meble.pl odrzuca na nich
    # cały plik.
    leftovers = len(re.findall(rb"<koszt_[1-4]>", new_blob))
    if leftovers:
        fail(
            f"W pliku wynikowym zostało {leftovers} elementów <koszt_N>. "
            "Walidator Meble.pl to odrzuci. Nie nadpisuję feed.xml."
        )

    old_blob = OUT.read_bytes() if OUT.exists() else b""
    if old_blob and strip_timestamp(old_blob) == strip_timestamp(new_blob):
        summary(
            [
                "## Feed bez zmian",
                "",
                f"- Źródło wygenerowane: `{created}`",
                f"- Pozycji w źródle: {total}, do publikacji: {len(kept)}",
                "- Treść identyczna z opublikowaną, `feed.xml` nietknięty.",
            ]
        )
        print("::notice::Brak zmian w treści feedu - nic nie commituję.")
        return

    OUT.write_bytes(new_blob)

    summary(
        [
            "## Feed zaktualizowany",
            "",
            f"- Tryb: `{MODE}`",
            f"- Źródło wygenerowane: `{created}`",
            f"- Pozycji w źródle: **{total}**",
            f"- Opublikowanych: **{len(kept)}**",
            f"- Przekonwertowanych koszty_transportu: {converted}",
            f"- Odrzuconych bez kategorii: {dropped['category']}",
            f"- Odrzuconych z blocklisty: {dropped['blocked']}",
            f"- Odrzuconych jako uszkodzone: {dropped['broken']}",
            f"- Rozmiar `feed.xml`: {len(new_blob):,} B",
        ]
    )


if __name__ == "__main__":
    main()
