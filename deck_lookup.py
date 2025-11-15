import argparse
import re
import sys
from typing import Dict, Iterable, List, Tuple

import requests
from hearthstone import deckstrings
from hearthstone.enums import FormatType


CARD_JSON_BASE = "https://api.hearthstonejson.com/v1/latest/{locale}/{filename}"
CARD_FILES = {
    True: "cards.collectible.json",
    False: "cards.json",
}
TAG_RE = re.compile(r"<[^>]+>")


def fetch_cards(locale: str, collectible_only: bool) -> List[dict]:
    """Download card data for the specified locale."""
    filename = CARD_FILES[collectible_only]
    url = CARD_JSON_BASE.format(locale=locale, filename=filename)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def build_lookup(cards: Iterable[dict]) -> Dict[int, dict]:
    """Index cards by dbfId for quick lookups."""
    lookup: Dict[int, dict] = {}
    for card in cards:
        dbf_id = card.get("dbfId")
        if dbf_id is None:
            continue
        lookup[int(dbf_id)] = card
    return lookup


def strip_tags(text: str) -> str:
    """Remove Hearthstone markup, brackets and redundant whitespace."""
    cleaned = TAG_RE.sub("", text)
    cleaned = cleaned.replace("[x]", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def describe_card(card: dict, copies: int) -> str:
    """Return a single line description for a card."""
    name = card.get("name", "Unknown")
    cost = card.get("cost", "?")
    ctype = card.get("type", "?").title()
    text = strip_tags(card.get("text", "")) if card.get("text") else ""
    if text:
        return f"({cost}) {name} x{copies} [{ctype}] - {text}"
    return f"({cost}) {name} x{copies} [{ctype}]"


def describe_hero(card: dict) -> str:
    hero_class = card.get("cardClass", "UNKNOWN")
    hero_class = hero_class.replace("DEMONHUNTER", "DEMON HUNTER")  # readability
    name = card.get("name", "Unknown Hero")
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


def print_deck(deck: deckstrings.Deck, cards: Dict[int, dict]) -> None:
    print(f"Deck format: {format_name(deck.format)}")
    hero_lines: List[str] = []
    for hero_id in deck.heroes:
        hero = cards.get(hero_id)
        if hero:
            hero_lines.append(describe_hero(hero))
        else:
            hero_lines.append(f"Unknown hero (dbfId={hero_id})")
    if hero_lines:
        print("Hero:", " / ".join(hero_lines))

    print("\nMain deck:")
    for dbf_id, copies in sorted(deck.cards, key=lambda item: (cards.get(item[0], {}).get("cost", 0), item[0])):
        card = cards.get(dbf_id)
        if card is None:
            print(f"  - Unknown card (dbfId={dbf_id}) x{copies}")
            continue
        print(f"  - {describe_card(card, copies)}")

    if deck.sideboards:
        print("\nSideboards:")
        sideboard_map: Dict[int, List[Tuple[int, int]]] = {}
        for card_id, quantity, owner in deck.sideboards:
            sideboard_map.setdefault(owner, []).append((card_id, quantity))
        for owner_id, contents in sideboard_map.items():
            owner_name = cards.get(owner_id, {}).get("name", f"dbfId={owner_id}")
            print(f"  * Sideboard for {owner_name}:")
            for card_id, count in contents:
                card = cards.get(card_id)
                if card:
                    print(f"      - {describe_card(card, count)}")
                else:
                    print(f"      - Unknown card (dbfId={card_id}) x{count}")


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
        help="Use cards.json (all cards) instead of cards.collectible.json.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> None:
    args = parse_args(argv)
    cards = fetch_cards(args.locale, collectible_only=not args.all_cards)
    card_lookup = build_lookup(cards)
    deck = decode_deck(args.deck_code.strip())
    print_deck(deck, card_lookup)


if __name__ == "__main__":
    main(sys.argv[1:])
