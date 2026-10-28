"""Free-review sessions on top of Anki's filtered-deck preview mode.

A session is a filtered deck with ``reschedule`` off, which is the same
mechanism as Custom Study when "Reschedule cards based on my answers in
this deck" is unchecked. Answering uses preview scheduling: the card's
real queue is restored when it leaves the deck, and the revlog row is
type cram (filtered), which FSRS does not treat as a review.

Two Anki behaviors still need a local fix so the collection matches the
pre-session card:

* Preview answers rewrite the stored FSRS ``decay`` to the current
  scheduler default. Stability and difficulty stay put; we still write
  the original decay back.
* A card whose real due value is 0 is not restored when it leaves a
  filtered deck (Anki treats original-due 0 as "unset"). Again/Hard in
  preview then leave a timestamp in ``due``. We write the snapshotted
  scheduling fields back when reps, interval, ease, lapses, and type
  still match, so a later real review is never overwritten.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from anki.cards import FSRSMemoryState
from anki.collection import Collection, OpChanges, SearchNode
from anki.decks import DeckId, FilteredDeckConfig
from anki.errors import FilteredDeckError, NotFoundError
from anki.scheduler import FilteredDeckForUpdate

FREE_REVIEW_PREFIX = "Free Review - "
# Anki custom study uses these preview delays when reschedule is off.
# Again/Hard show the card again inside this session only; Good returns
# it. Easy always returns the card (there is no preview-easy delay).
PREVIEW_AGAIN_SECS = 60
PREVIEW_HARD_SECS = 600
PREVIEW_GOOD_SECS = 0
PREVIEW_DELAY_SECS = 10

EMPTY_MESSAGE = "No seen cards to practice in this deck."
NONE_MOVABLE_MESSAGE = (
    "No seen cards can be practiced right now. Suspended cards, buried "
    "cards, and cards already in another filtered deck are skipped."
)
FILTERED_SOURCE_MESSAGE = "Free Review works on a normal deck, not a filtered deck."
MISSING_DECK_MESSAGE = "That deck no longer exists."
V3_MESSAGE = "Free Review needs Anki's V3 scheduler (Anki 23.10 or newer)."

_SNAPSHOT_VERSION = 1
_SEARCH_MARKERS = ("-is:new", "-is:suspended", "-is:buried")


@dataclass
class StartResult:
    """Outcome of trying to open a free-review filtered deck.

    ``changes`` is the OpChanges from the filtered-deck build, or an empty
    OpChanges when nothing was written. aqt's CollectionOp reads this
    attribute to refresh the UI.
    """

    ok: bool
    deck_id: int | None
    message: str
    card_count: int
    changes: OpChanges


@dataclass
class CardSnapshot:
    did: int
    type: int
    queue: int
    due: int
    ivl: int
    factor: int
    reps: int
    lapses: int
    left: int
    odue: int
    odid: int
    custom_data: str
    stability: float | None
    difficulty: float | None
    decay: float | None
    desired_retention: float | None
    last_review_time: int | None


def seen_card_search(col: Collection, deck_name: str) -> str:
    """Search for seen cards in ``deck_name`` and its subdecks.

    Suspended and buried cards are excluded. Anki's filtered-deck builder
    also refuses to gather them, so the search matches what the deck can
    actually hold.
    """

    return col.build_search_string(
        SearchNode(deck=deck_name),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_NEW)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_SUSPENDED)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_BURIED)),
    )


def snapshot_path(col: Collection) -> str:
    """Local restore file beside the collection. AnkiWeb does not sync it."""

    return col.path + ".free-review.json"


def start_free_review(col: Collection, source_deck_id: int) -> StartResult:
    """Create the temporary filtered deck and gather seen cards into it.

    Returns an unsuccessful result, and does not leave a deck behind, when
    there is nothing to practice.
    """

    if not col.v3_scheduler():
        return _fail(V3_MESSAGE)

    source_deck_id = int(source_deck_id)
    if not col.decks.have(DeckId(source_deck_id)):
        return _fail(MISSING_DECK_MESSAGE)
    if col.decks.is_filtered(DeckId(source_deck_id)):
        return _fail(FILTERED_SOURCE_MESSAGE)

    # Finish any previous practice deck before snapshotting, so a crashed
    # session cannot be captured as the new baseline.
    cleanup_all_free_review_sessions(col)

    deck_name = col.decks.name(DeckId(source_deck_id))
    search = seen_card_search(col, deck_name)
    card_ids = list(col.find_cards(search))
    if not card_ids:
        return _fail(EMPTY_MESSAGE)

    filtered_name, existing_id = _allocate_filtered_name(col, deck_name)
    snapshots = {
        int(cid): _snapshot_card(col.get_card(cid)) for cid in card_ids
    }
    _store_session(
        col,
        filtered_name,
        source_deck_id=source_deck_id,
        filtered_deck_id=existing_id or None,
        snapshots=snapshots,
    )

    config = FilteredDeckConfig(
        reschedule=False,
        search_terms=[
            FilteredDeckConfig.SearchTerm(
                search=search,
                limit=len(card_ids),
                order=FilteredDeckConfig.SearchTerm.Order.RANDOM,
            )
        ],
        preview_delay=PREVIEW_DELAY_SECS,
        preview_again_secs=PREVIEW_AGAIN_SECS,
        preview_hard_secs=PREVIEW_HARD_SECS,
        preview_good_secs=PREVIEW_GOOD_SECS,
    )
    try:
        out = col.sched.add_or_update_filtered_deck(
            _filtered_update(existing_id, filtered_name, config)
        )
    except FilteredDeckError:
        _drop_session(col, filtered_name)
        return _fail(NONE_MOVABLE_MESSAGE)
    except Exception:
        _drop_session(col, filtered_name)
        raise

    filtered_id = int(out.id)
    _store_session(
        col,
        filtered_name,
        source_deck_id=source_deck_id,
        filtered_deck_id=filtered_id,
        snapshots=snapshots,
    )
    if not _is_our_deck(col, filtered_id):
        # The build did not land on the preview deck we asked for.
        if col.decks.have(DeckId(filtered_id)):
            col.decks.remove([DeckId(filtered_id)])
        _drop_session(col, filtered_name)
        return _fail(NONE_MOVABLE_MESSAGE)

    gathered = list(col.find_cards(f"did:{filtered_id}"))
    if not gathered:
        cleanup_free_review_deck(col, filtered_id)
        return _fail(NONE_MOVABLE_MESSAGE)

    return StartResult(
        ok=True,
        deck_id=filtered_id,
        message="",
        card_count=len(gathered),
        changes=out.changes,
    )


def cleanup_free_review_deck(col: Collection, deck_id: int) -> bool:
    """Return cards home, delete one practice deck, and restore snapshots."""

    deck_id = int(deck_id)
    data = _load_store(col)
    name = col.decks.name(DeckId(deck_id)) if col.decks.have(DeckId(deck_id)) else None
    keys = [
        key
        for key, session in data["sessions"].items()
        if session.get("filtered_deck_id") == deck_id or key == name
    ]
    removed = False
    if name is not None and _is_our_deck(col, deck_id):
        col.decks.remove([DeckId(deck_id)])
        removed = True
    for key in keys:
        _restore_cards(col, data["sessions"][key]["cards"])
        del data["sessions"][key]
    if keys:
        _write_store(col, data)
    return removed or bool(keys)


def cleanup_all_free_review_sessions(col: Collection) -> int:
    """Remove every practice deck this addon created and apply snapshots once.

    Safe to call on profile open, before sync, and when a session ends.
    Cards that have since received a real review (reps, interval, ease,
    lapses, or type changed) are left alone.
    """

    removed = 0
    our_ids = [
        int(item.id)
        for item in col.decks.all_names_and_ids()
        if _is_our_deck(col, int(item.id))
    ]
    for deck_id in our_ids:
        col.decks.remove([DeckId(deck_id)])
        removed += 1

    data = _load_store(col)
    if data["sessions"]:
        for session in data["sessions"].values():
            _restore_cards(col, session["cards"])
        data["sessions"] = {}
        _write_store(col, data)
    elif removed:
        _write_store(col, data)
    return removed


def is_free_review_deck(col: Collection, deck_id: int) -> bool:
    return _is_our_deck(col, int(deck_id))


def _fail(message: str) -> StartResult:
    return StartResult(
        ok=False, deck_id=None, message=message, card_count=0, changes=OpChanges()
    )


def _filtered_update(existing_id: int, name: str, config: FilteredDeckConfig):
    return FilteredDeckForUpdate(
        id=existing_id,
        name=name,
        config=config,
        allow_empty=False,
    )


def _display_deck_name(deck_name: str) -> str:
    # "::" would make Anki nest the filtered deck under a parent.
    return deck_name.replace("::", " / ")


def _allocate_filtered_name(col: Collection, source_name: str) -> tuple[str, int]:
    base = f"{FREE_REVIEW_PREFIX}{_display_deck_name(source_name)}"
    for index in range(1, 50):
        name = base if index == 1 else f"{base} ({index})"
        existing = col.decks.id_for_name(name)
        if existing is None:
            return name, 0
        if _is_our_deck(col, int(existing)):
            return name, int(existing)
    raise RuntimeError(f"no free name for a practice deck based on {base!r}")


def _is_our_deck(col: Collection, deck_id: int) -> bool:
    if not col.decks.have(DeckId(deck_id)):
        return False
    if not col.decks.is_filtered(DeckId(deck_id)):
        return False
    name = col.decks.name(DeckId(deck_id))
    if not name.startswith(FREE_REVIEW_PREFIX):
        return False
    try:
        deck = col.sched.get_or_create_filtered_deck(DeckId(deck_id))
    except Exception:
        return False
    if deck.config.reschedule:
        return False
    search = " ".join(term.search for term in deck.config.search_terms)
    return all(marker in search for marker in _SEARCH_MARKERS)


def _snapshot_card(card) -> CardSnapshot:
    memory = card.memory_state
    return CardSnapshot(
        did=int(card.did),
        type=int(card.type),
        queue=int(card.queue),
        due=int(card.due),
        ivl=int(card.ivl),
        factor=int(card.factor),
        reps=int(card.reps),
        lapses=int(card.lapses),
        left=int(card.left),
        odue=int(card.odue),
        odid=int(card.odid),
        custom_data=card.custom_data or "",
        stability=None if memory is None else memory.stability,
        difficulty=None if memory is None else memory.difficulty,
        decay=card.decay,
        desired_retention=card.desired_retention,
        last_review_time=card.last_review_time,
    )


def _apply_snapshot(card, snap: CardSnapshot) -> None:
    card.did = snap.did
    card.type = snap.type
    card.queue = snap.queue
    card.due = snap.due
    card.ivl = snap.ivl
    card.factor = snap.factor
    card.reps = snap.reps
    card.lapses = snap.lapses
    card.left = snap.left
    card.odue = snap.odue
    card.odid = snap.odid
    card.custom_data = snap.custom_data
    card.decay = snap.decay
    card.desired_retention = snap.desired_retention
    card.last_review_time = snap.last_review_time
    if snap.stability is None or snap.difficulty is None:
        card.memory_state = None
    else:
        card.memory_state = FSRSMemoryState(
            stability=snap.stability, difficulty=snap.difficulty
        )


def _unchanged_by_real_review(card, snap: CardSnapshot) -> bool:
    """True when a normal review has not rewritten the scheduling identity.

    Preview answers do not change these. A real review increments reps and
    rewrites interval, ease, lapses, or type.
    """

    return (
        int(card.reps) == snap.reps
        and int(card.lapses) == snap.lapses
        and int(card.ivl) == snap.ivl
        and int(card.factor) == snap.factor
        and int(card.type) == snap.type
    )


def _matches(card, snap: CardSnapshot) -> bool:
    memory = card.memory_state
    stability = None if memory is None else memory.stability
    difficulty = None if memory is None else memory.difficulty
    return _unchanged_by_real_review(card, snap) and (
        int(card.did) == snap.did
        and int(card.queue) == snap.queue
        and int(card.due) == snap.due
        and int(card.left) == snap.left
        and int(card.odue) == snap.odue
        and int(card.odid) == snap.odid
        and (card.custom_data or "") == snap.custom_data
        and card.decay == snap.decay
        and card.desired_retention == snap.desired_retention
        and card.last_review_time == snap.last_review_time
        and stability == snap.stability
        and difficulty == snap.difficulty
    )


def _restore_cards(col: Collection, cards: dict) -> None:
    dirty = []
    for cid_text, payload in cards.items():
        try:
            card = col.get_card(int(cid_text))
        except (NotFoundError, TypeError, ValueError):
            continue
        snap = CardSnapshot(**payload)
        if not _unchanged_by_real_review(card, snap):
            continue
        if _matches(card, snap):
            continue
        _apply_snapshot(card, snap)
        dirty.append(card)
    if dirty:
        col.update_cards(dirty)


def _store_session(
    col: Collection,
    name: str,
    *,
    source_deck_id: int,
    filtered_deck_id: int | None,
    snapshots: dict[int, CardSnapshot],
) -> None:
    data = _load_store(col)
    data["sessions"][name] = {
        "source_deck_id": source_deck_id,
        "filtered_deck_id": filtered_deck_id,
        "cards": {str(cid): asdict(snap) for cid, snap in snapshots.items()},
    }
    _write_store(col, data)


def _drop_session(col: Collection, name: str) -> None:
    data = _load_store(col)
    if name in data["sessions"]:
        del data["sessions"][name]
        _write_store(col, data)


def _empty_store() -> dict:
    return {"version": _SNAPSHOT_VERSION, "sessions": {}}


def _load_store(col: Collection) -> dict:
    path = snapshot_path(col)
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict) or data.get("version") != _SNAPSHOT_VERSION:
        return _empty_store()
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return _empty_store()
    return {"version": _SNAPSHOT_VERSION, "sessions": sessions}


def _write_store(col: Collection, data: dict) -> None:
    path = snapshot_path(col)
    if not data.get("sessions"):
        if os.path.exists(path):
            os.remove(path)
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    os.replace(tmp, path)
