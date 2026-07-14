"""Closed-world ontology for deterministic Phase E historical enrichment.

Only identifiers and labels declared here may leave the ephemeral text classifier.
The ontology deliberately separates interests, recurring activities, and typed
public entities so a mention does not silently become a positive preference.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Final

ONTOLOGY_VERSION: Final = "phase-e-ontology-v3.0.0"
RULE_VERSION: Final = "phase-e-rules-v3.0.0"


@dataclass(frozen=True)
class TaxonomyRule:
    ontology_id: str
    label: str
    lane: str
    phrases: tuple[str, ...]
    exclusions: tuple[str, ...] = ()
    context_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicEntity:
    canonical_label: str
    entity_type: str
    aliases: tuple[str, ...]


# Phrases are ordered most-specific first. Matching code independently enforces
# longest-phrase precedence and word boundaries.
TAXONOMY: Final = (
    TaxonomyRule("music.electronic", "electronic dance music", "interest", (
        "electronic dance music", "house music", "techno", "trance", "edm", "rave", "dj set",
    ), exclusions=("house chores", "house keys", "at the house")),
    TaxonomyRule("music.general", "music", "interest", (
        "music festival", "music video", "live music", "concert", "playlist", "album", "song", "track", "band", "singer", "rapper", "artist",
    )),
    TaxonomyRule("events.music", "concerts and music festivals", "activity", (
        "music festival", "festival lineup", "concert tickets", "concert", "festival", "rave tickets",
    )),
    TaxonomyRule("screen.movies", "movies", "interest", (
        "movie night", "movie theater", "movie", "movies", "film", "cinema", "trailer",
    )),
    TaxonomyRule("screen.television", "television series", "interest", (
        "tv series", "television series", "watch the show", "watching the show", "episode", "episodes", "season finale", "series", "show",
    ), exclusions=("show me", "show you", "show up", "showed up")),
    TaxonomyRule("training.strength", "bodybuilding and strength training", "interest", (
        "strength training", "weight training", "bodybuilding", "bodybuilder", "powerlifting", "hypertrophy", "lifting weights", "lift weights", "workout", "workouts", "lifting", "gym", "leg day", "chest day", "back day", "arm day", "squat", "bench press", "deadlift",
    )),
    TaxonomyRule("training.cardio", "cardio training", "interest", (
        "cardio workout", "cardio", "treadmill", "stairmaster", "elliptical", "running", "jogging", "run miles",
    )),
    TaxonomyRule("training.activity", "training sessions", "activity", (
        "going to the gym", "at the gym", "gym session", "training session", "workout", "workouts", "lifting", "cardio",
    )),
    TaxonomyRule("nutrition.fitness", "bodybuilding nutrition", "interest", (
        "protein shake", "protein powder", "meal prep", "macros", "creatine", "pre workout", "post workout", "calorie deficit", "bulking", "cutting", "supplements",
    )),
    TaxonomyRule("food.dining", "food and dining", "interest", (
        "restaurant", "restaurants", "dinner", "lunch", "breakfast", "brunch", "takeout", "take out", "meal", "meals", "food", "cuisine", "cooking", "recipe", "eat", "eating",
    )),
    TaxonomyRule("food.outing", "meals and restaurant outings", "activity", (
        "go to dinner", "going to dinner", "get dinner", "grab dinner", "go to lunch", "get lunch", "brunch", "restaurant", "takeout",
    )),
    TaxonomyRule("travel.general", "travel", "interest", (
        "international travel", "road trip", "vacation", "travel", "trip", "destination", "tourism", "airport", "flight", "flying", "hotel", "airbnb", "beach trip",
    )),
    TaxonomyRule("travel.activity", "trips and travel", "activity", (
        "road trip", "vacation", "trip", "flight", "flying", "airport", "hotel", "traveling", "travelling",
    )),
    TaxonomyRule("vehicles.general", "vehicles", "interest", (
        "sports car", "electric car", "electric vehicle", "tesla", "model x", "model y", "vehicle", "vehicles", "car", "cars", "truck", "motorcycle",
    )),
    TaxonomyRule("vehicles.driving", "driving", "activity", (
        "road trip", "driving", "drive", "supercharger", "charging the car", "car wash",
    )),
    TaxonomyRule("vehicles.service", "vehicle service", "activity", (
        "service center", "car service", "vehicle service", "tire rotation", "new tires", "oil change", "repair shop", "mechanic", "charging appointment",
    )),
    TaxonomyRule("technology.ai", "artificial intelligence", "interest", (
        "artificial intelligence", "machine learning", "chatgpt", "claude code", "claude", "openai", "llm", "ai model", "ai models",
    )),
    TaxonomyRule("technology.computing", "computing and technology", "interest", (
        "computer", "computers", "software", "coding", "programming", "iphone", "ipad", "macbook", "android", "gadget", "technology", "tech",
    )),
    TaxonomyRule("games.video", "video games", "interest", (
        "video game", "video games", "gaming", "playstation", "xbox", "nintendo", "steam game", "pc game",
    )),
    TaxonomyRule("outings.walks", "walks and hikes", "activity", (
        "go for a walk", "going for a walk", "take a walk", "walk the dog", "dog walk", "walking", "hiking", "hike", "trail walk",
    )),
    TaxonomyRule("outings.social", "social outings", "activity", (
        "night out", "going out", "go out tonight", "hang out", "hanging out", "meet up", "party", "club night", "outing", "museum", "theater",
    )),
    TaxonomyRule("plans.tickets", "ticketed events", "activity", (
        "concert tickets", "festival tickets", "movie tickets", "game tickets", "tickets", "ticket",
    )),
)

# Entity admission is intentionally finite. Personal names and arbitrary NER
# spans are never admitted. Additions require a code review and version bump.
PUBLIC_ENTITIES: Final = (
    PublicEntity("Lady Gaga", "music_artist", ("lady gaga", "gaga")),
    PublicEntity("Beyonce", "music_artist", ("beyonce", "beyoncé")),
    PublicEntity("Madonna", "music_artist", ("madonna",)),
    PublicEntity("Charli XCX", "music_artist", ("charli xcx",)),
    PublicEntity("Troye Sivan", "music_artist", ("troye sivan",)),
    PublicEntity("Ariana Grande", "music_artist", ("ariana grande",)),
    PublicEntity("Dua Lipa", "music_artist", ("dua lipa",)),
    PublicEntity("Taylor Swift", "music_artist", ("taylor swift",)),
    PublicEntity("The Weeknd", "music_artist", ("the weeknd", "weeknd")),
    PublicEntity("EDC", "music_event", ("electric daisy carnival", "edc")),
    PublicEntity("Ultra Music Festival", "music_event", ("ultra music festival", "ultra festival")),
    PublicEntity("Tomorrowland", "music_event", ("tomorrowland",)),
    PublicEntity("Boston", "place", ("boston",)),
    PublicEntity("Florida", "place", ("florida",)),
    PublicEntity("Providence", "place", ("providence",)),
    PublicEntity("Miami", "place", ("miami",)),
    PublicEntity("New York City", "place", ("new york city", "nyc")),
    PublicEntity("Tesla", "vehicle_brand", ("tesla",)),
    PublicEntity("Tesla Model X", "vehicle_model", ("tesla model x", "model x")),
    PublicEntity("Tesla Model Y", "vehicle_model", ("tesla model y", "model y")),
    PublicEntity("PlayStation", "game_platform", ("playstation", "ps5")),
    PublicEntity("Xbox", "game_platform", ("xbox",)),
    PublicEntity("Nintendo Switch", "game_platform", ("nintendo switch", "switch game")),
)

PLATFORM_ENTITY_DENYLIST: Final = frozenset({
    "apple", "discord", "facebook", "google", "imessage", "instagram", "netflix",
    "reddit", "spotify", "telegram", "tiktok", "twitter", "x", "youtube",
})


def _payload() -> dict[str, object]:
    return {
        "ontology_version": ONTOLOGY_VERSION,
        "rule_version": RULE_VERSION,
        "taxonomy": [rule.__dict__ for rule in TAXONOMY],
        "public_entities": [entity.__dict__ for entity in PUBLIC_ENTITIES],
        "platform_denylist": sorted(PLATFORM_ENTITY_DENYLIST),
    }


TAXONOMY_COMMITMENT: Final = hashlib.sha256(
    json.dumps(_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

__all__ = [
    "ONTOLOGY_VERSION", "PLATFORM_ENTITY_DENYLIST", "PUBLIC_ENTITIES", "RULE_VERSION",
    "TAXONOMY", "TAXONOMY_COMMITMENT", "PublicEntity", "TaxonomyRule",
]
