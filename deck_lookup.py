import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests
from hearthstone import deckstrings
from hearthstone.enums import FormatType


CARD_JSON_BASE = "https://api.hearthstonejson.com/v1/latest/{locale}/{filename}"
CARD_BUILD_BASE = "https://api.hearthstonejson.com/v1/{build}/{locale}/{filename}"
CARD_FILES = {
    True: "cards.collectible.json",
    False: "cards.json",
}
TAG_RE = re.compile(r"<[^>]+>")
DERIVED_CARD_TYPES = {"MINION", "SPELL", "WEAPON", "LOCATION", "HERO", "HERO_POWER"}
_DERIVED_CACHE: Dict[str, List[dict]] = {}
CACHE_ROOT = Path(".cache/hearthstonejson")
_LATEST_BUILD_CACHE: Dict[Tuple[str, str], Tuple[str, str]] = {}


def fetch_cards(locale: str, collectible_only: bool) -> List[dict]:
    """Download card data for the specified locale."""
    filename = CARD_FILES[collectible_only]
    cached_file = ensure_cached_file(locale, filename)
    with cached_file.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_latest_build(locale: str, filename: str) -> Tuple[str, str]:
    """Return the latest build id and resolved URL for a given file."""
    key = (locale, filename)
    if key in _LATEST_BUILD_CACHE:
        return _LATEST_BUILD_CACHE[key]

    url = CARD_JSON_BASE.format(locale=locale, filename=filename)
    resp = requests.head(url, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    resolved_url = resp.url.rstrip("/")
    parts = resolved_url.split("/")
    try:
        idx = parts.index("v1")
        build_id = parts[idx + 1]
    except (ValueError, IndexError) as exc:  # pragma: no cover - paranoid guard
        raise RuntimeError(f"Unable to determine build from URL: {resolved_url}") from exc

    _LATEST_BUILD_CACHE[key] = (build_id, resolved_url)
    return build_id, resolved_url


def ensure_cached_file(locale: str, filename: str) -> Path:
    """Ensure the latest card file exists on disk and return its path."""
    build_id, resolved_url = resolve_latest_build(locale, filename)
    cache_dir = CACHE_ROOT / build_id / locale
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / filename

    if cache_file.exists():
        return cache_file

    # Need to download the current version
    download_url = CARD_BUILD_BASE.format(build=build_id, locale=locale, filename=filename)
    resp = requests.get(download_url, timeout=120)
    resp.raise_for_status()
    cache_file.write_bytes(resp.content)
    return cache_file


def build_lookups(cards: Iterable[dict]) -> Tuple[Dict[int, dict], Dict[str, dict]]:
    """Index cards by dbfId and card id for quick lookups."""
    by_dbf: Dict[int, dict] = {}
    by_id: Dict[str, dict] = {}
    for card in cards:
        dbf_id = card.get("dbfId")
        if dbf_id is not None:
            by_dbf[int(dbf_id)] = card
        card_id = card.get("id")
        if card_id:
            by_id[card_id] = card
    return by_dbf, by_id


def merge_id_lookup(base: Dict[str, dict], other: Dict[str, dict]) -> Dict[str, dict]:
    """Merge another id->card mapping, keeping existing entries when conflicts occur."""
    merged = dict(base)
    for cid, card in other.items():
        if cid not in merged:
            merged[cid] = card
    return merged


def strip_tags(text: str) -> str:
    """Remove Hearthstone markup, brackets and redundant whitespace."""
    cleaned = TAG_RE.sub("", text)
    cleaned = cleaned.replace("[x]", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def collect_prefixed_cards(card_id: str, cards_by_id: Dict[str, dict]) -> List[dict]:
    if not card_id:
        return []
    if card_id in _DERIVED_CACHE:
        return _DERIVED_CACHE[card_id]

    related: List[dict] = []
    seen: set[str] = set()
    for other_id, other_card in cards_by_id.items():
        if other_id == card_id or not other_id.startswith(card_id):
            continue
        if other_card.get("type") not in DERIVED_CARD_TYPES:
            continue
        if other_id in seen:
            continue
        seen.add(other_id)
        related.append(other_card)

    related.sort(key=lambda c: (len(c.get("id", "")), c.get("cost", 0), c.get("name", "")))
    _DERIVED_CACHE[card_id] = related
    return related


def describe_card(
    card: dict, copies: int, cards_by_id: Dict[str, dict]
) -> Tuple[str, List[str], dict]:
    """Return formatted description plus optional derived lines and structured info."""
    name = card.get("name", "Unknown")
    cost = card.get("cost", "?")
    ctype = card.get("type", "?").title()
    text = strip_tags(card.get("text", "")) if card.get("text") else ""
    main_line = f"({cost}) {name} x{copies} [{ctype}]"
    if text:
        main_line = f"{main_line} - {text}"

    derived_cards = collect_prefixed_cards(card.get("id", ""), cards_by_id)
    derived_lines: List[str] = []
    derived_entries: List[dict] = []
    for related in derived_cards:
        related_name = related.get("name", related.get("id", "Unknown"))
        related_type = related.get("type", "?").title()
        related_text = strip_tags(related.get("text", "")) if related.get("text") else ""
        extra = f" -> {related_name} [{related_type}]"
        if related_text:
            extra = f"{extra} - {related_text}"
        derived_lines.append(extra)
        derived_entries.append(
            {
                "id": related.get("id"),
                "dbfId": related.get("dbfId"),
                "name": related_name,
                "type": related_type,
                "cost": related.get("cost"),
                "text": related_text,
            }
        )

    card_info = {
        "dbfId": card.get("dbfId"),
        "id": card.get("id"),
        "name": name,
        "type": ctype,
        "cost": cost,
        "copies": copies,
        "text": text,
        "summary": main_line,
        "derivedSummaries": derived_lines,
        "derived": derived_entries,
    }

    return main_line, derived_lines, card_info


def describe_hero(card: dict) -> str:
    hero_class = card.get("cardClass", "UNKNOWN") if card else "UNKNOWN"
    hero_class = hero_class.replace("DEMONHUNTER", "DEMON HUNTER")
    name = card.get("name", "Unknown Hero") if card else "Unknown Hero"
    return f"{name} ({hero_class.title()})"


def format_name(deck_format: FormatType) -> str:
    if isinstance(deck_format, FormatType):
        return deck_format.name.replace("FT_", "").title()
    return str(deck_format)


def decode_deck(deck_code: str) -> deckstrings.Deck:
    try:
        return deckstrings.Deck.from_deckstring(deck_code)
    except Exception as exc:  # pragma: no cover - runtime guard
        raise SystemExit(f"Failed to decode deck code: {exc}") from exc


def summarize_deck(
    deck: deckstrings.Deck,
    cards_by_dbf: Dict[int, dict],
    cards_by_id: Dict[str, dict],
    deck_code: str,
) -> dict:
    report = {
        "deckCode": deck_code,
        "format": format_name(deck.format),
        "heroes": [],
        "cards": [],
        "sideboards": [],
    }

    for hero_id in deck.heroes:
        hero = cards_by_dbf.get(hero_id)
        summary = describe_hero(hero) if hero else f"Unknown hero (dbfId={hero_id})"
        report["heroes"].append(
            {
                "dbfId": hero_id,
                "id": hero.get("id") if hero else None,
                "name": hero.get("name") if hero else None,
                "cardClass": hero.get("cardClass") if hero else None,
                "summary": summary,
            }
        )

    sorted_cards = sorted(
        deck.cards, key=lambda item: (cards_by_dbf.get(item[0], {}).get("cost", 0), item[0])
    )
    for dbf_id, copies in sorted_cards:
        card = cards_by_dbf.get(dbf_id)
        if card is None:
            summary = f"Unknown card (dbfId={dbf_id}) x{copies}"
            report["cards"].append(
                {
                    "dbfId": dbf_id,
                    "id": None,
                    "name": None,
                    "type": None,
                    "cost": None,
                    "copies": copies,
                    "text": "",
                    "missing": True,
                    "summary": summary,
                    "derivedSummaries": [],
                    "derived": [],
                }
            )
            continue

        _, _, card_info = describe_card(card, copies, cards_by_id)
        report["cards"].append(card_info)

    if deck.sideboards:
        sideboard_map: Dict[int, List[Tuple[int, int]]] = {}
        for card_id, quantity, owner in deck.sideboards:
            sideboard_map.setdefault(owner, []).append((card_id, quantity))
        for owner_id, contents in sideboard_map.items():
            owner_card = cards_by_dbf.get(owner_id)
            owner_name = owner_card.get("name") if owner_card else f"dbfId={owner_id}"
            sideboard_entry = {
                "ownerDbfId": owner_id,
                "ownerName": owner_name,
                "cards": [],
            }
            for card_id, count in contents:
                card = cards_by_dbf.get(card_id)
                if card:
                    _, _, card_info = describe_card(card, count, cards_by_id)
                    sideboard_entry["cards"].append(card_info)
                else:
                    sideboard_entry["cards"].append(
                        {
                            "dbfId": card_id,
                            "id": None,
                            "name": None,
                            "type": None,
                            "cost": None,
                            "copies": count,
                            "text": "",
                            "missing": True,
                            "summary": f"Unknown card (dbfId={card_id}) x{count}",
                            "derivedSummaries": [],
                            "derived": [],
                        }
                    )
            report["sideboards"].append(sideboard_entry)

    return report


def print_deck(report: dict) -> None:
    print(f"Deck format: {report['format']}")
    if report["heroes"]:
        hero_line = " / ".join(hero["summary"] for hero in report["heroes"])
        print("Hero:", hero_line)

    print("\nMain deck:")
    for card in report["cards"]:
        print(f"  - {card['summary']}")
        for extra in card["derivedSummaries"]:
            print(f"      {extra}")

    if report["sideboards"]:
        print("\nSideboards:")
        for sideboard in report["sideboards"]:
            print(f"  * Sideboard for {sideboard['ownerName']}:")
            for card in sideboard["cards"]:
                print(f"      - {card['summary']}")
                for extra in card["derivedSummaries"]:
                    print(f"          {extra}")


def safe_filename(name: str, fallback: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        candidate = fallback.strip() or "deck"
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-_ ]+", "_", candidate)
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned or fallback or "deck"


def write_deck_json(report: dict, deck_name: str | None) -> Path:
    safe_name = safe_filename(deck_name or "", report["deckCode"][:12])
    output_path = Path(f"{safe_name}.json")
    payload = dict(report)
    payload["name"] = deck_name or safe_name
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download HearthstoneJSON card data and show the details for a deck code."
    )
    parser.add_argument("deck_code", help="Hearthstone deck code string")
    parser.add_argument(
        "--locale",
        default="zhCN",
        help="Locale to download (default: zhCN, e.g. enUS, zhTW)",
    )
    parser.add_argument(
        "--all-cards",
        action="store_true",
        help="Use cards.json for the main lookup (collectible by default).",
    )
    parser.add_argument(
        "--deck-name",
        help="Optional deck name used for the output JSON filename.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> None:
    args = parse_args(argv)
    cards = fetch_cards(args.locale, collectible_only=not args.all_cards)
    cards_by_dbf, cards_by_id = build_lookups(cards)

    if not args.all_cards:
        all_cards = fetch_cards(args.locale, collectible_only=False)
        _, all_cards_by_id = build_lookups(all_cards)
        cards_by_id = merge_id_lookup(cards_by_id, all_cards_by_id)

    deck_code = args.deck_code.strip()
    deck = decode_deck(deck_code)
    report = summarize_deck(deck, cards_by_dbf, cards_by_id, deck_code)
    print_deck(report)
    output_path = write_deck_json(report, args.deck_name)
    print(f"\nSaved deck details to {output_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
