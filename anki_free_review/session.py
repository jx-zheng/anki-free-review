"""Free-review sessions on top of Anki's filtered-deck preview mode.

A session is a filtered deck with ``reschedule`` off, which is the same
mechanism as Custom Study when "Reschedule cards based on my answers in
this deck" is unchecked. Answering uses preview scheduling. The revlog
row is type cram (filtered), which FSRS does not treat as a review.

A struggle revisit is the same kind of deck, limited to card ids. Anki still
sets ``odid`` to the home deck while the card sits in the practice deck.
Struggle totals are not written into this restore file.

Two Anki preview side effects are patched after the temporary deck is
removed. Nothing else on the card is written back:

* Due value 0 is not restored when a card leaves a filtered deck (Anki
  treats original-due 0 as unset). Again/Hard then leave a unix timestamp
  in ``due``. That timestamp is set back to 0.
* With FSRS on, a preview answer stores the current FSRS decay, and on
  some cards that had no memory state (a relearning card in 25.9.2) it
  writes stability and difficulty. Those FSRS fields are put back.
  Queue and deck id are not.

Suspend, bury, Set Due Date, and moving a card to another deck are not
undone. A real review (reps, interval, ease, lapses, or type changed) is
not undone either.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from anki.cards import FSRSMemoryState
from anki.collection import Collection, OpChanges, SearchNode
from anki.decks import DeckId, FilteredDeckConfig
from anki.errors import FilteredDeckError, InvalidInput, NotFoundError
from anki.scheduler import FilteredDeckForUpdate

logger = logging.getLogger(__name__)

FREE_REVIEW_PREFIX = "Free Review - "
STRUGGLE_NAME_SUFFIX = " (struggle)"
# Anki custom study uses these preview delays when reschedule is off.
# Again/Hard show the card again inside this session only; Good returns
# it. Easy always returns the card (there is no preview-easy delay).
PREVIEW_AGAIN_SECS = 60
PREVIEW_HARD_SECS = 600
PREVIEW_GOOD_SECS = 0
PREVIEW_DELAY_SECS = 10

EMPTY_MESSAGE = "No seen cards to practice in this deck."
NO_STRUGGLE_CARDS_MESSAGE = "No struggle cards to practice."
NONE_MOVABLE_MESSAGE = (
    "No seen cards can be practiced right now. Suspended cards, buried "
    "cards, and cards already in another filtered deck are skipped."
)
FILTERED_SOURCE_MESSAGE = "Free Review works on a normal deck, not a filtered deck."
MISSING_DECK_MESSAGE = "That deck no longer exists."
V3_MESSAGE = "Free Review needs Anki 25.9.2 or newer (V3 scheduler)."
SNAPSHOT_CORRUPT_MESSAGE = (
    "Free Review could not read its restore file, so it left cards and decks alone."
)

_SNAPSHOT_VERSION = 1
_SEARCH_MARKERS = ("-is:new", "-is:suspended", "-is:buried")
# Anki's browser calls a review card mature when its interval is at least 21 days.
# 25.9.2 has no is:mature / is:young search term.
MATURE_INTERVAL_DAYS = 21
SCOPE_ALL_SEEN = "all"
SCOPE_OMIT_MATURE = "omit_mature"
# Day-numbered dues stay far below this. Preview Again/Hard write a unix time.
_PREVIEW_DUE_TIMESTAMP = 1_000_000_000
_snapshot_warnings: list[str] = []


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


def seen_card_search(
    col: Collection, deck_name: str, *, omit_mature: bool = False
) -> str:
    """Search for seen cards in ``deck_name`` and its subdecks.

    Suspended and buried cards are excluded. Anki's filtered-deck builder
    also refuses to gather them, so the search matches what the deck can
    actually hold.

    When ``omit_mature`` is set, cards with interval >= 21 days are excluded
    (``-prop:ivl>=21``). Learning cards usually have interval 0, so they stay.
    A relearning card keeps its previous interval, so a lapsed mature card
    is excluded too.
    """

    search = col.build_search_string(
        SearchNode(deck=deck_name),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_NEW)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_SUSPENDED)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_BURIED)),
    )
    if omit_mature:
        search = col.build_search_string(
            search, f"-prop:ivl>={MATURE_INTERVAL_DAYS}"
        )
    return search


def snapshot_path(col: Collection) -> str:
    """Local restore file beside the collection. AnkiWeb does not sync it."""

    return col.path + ".free-review.json"


def take_snapshot_warnings() -> list[str]:
    """Return restore-file warnings recorded since the last call, then clear them."""

    found = list(_snapshot_warnings)
    _snapshot_warnings.clear()
    return found


def start_from_dialog_choice(
    col: Collection, source_deck_id: int, scope: str | None
) -> StartResult | None:
    """Start a session, or do nothing when the dialog was cancelled."""

    if scope is None:
        return None
    return start_free_review(col, source_deck_id, scope=scope)


def start_free_review(
    col: Collection, source_deck_id: int, *, scope: str = SCOPE_ALL_SEEN
) -> StartResult:
    """Create the temporary filtered deck and gather seen cards into it.

    ``scope`` is ``SCOPE_ALL_SEEN`` or ``SCOPE_OMIT_MATURE``. Returns an
    unsuccessful result, and does not leave a deck behind, when there is
    nothing to practice.
    """

    source_deck_id = int(source_deck_id)
    error = _source_error(col, source_deck_id)
    if error:
        return error

    # Finish any previous practice deck before snapshotting, so a crashed
    # session cannot be captured as the new baseline.
    cleanup_all_free_review_sessions(col)

    deck_name = col.decks.name(DeckId(source_deck_id))
    search = seen_card_search(col, deck_name, omit_mature=_omit_mature(scope))
    card_ids = list(col.find_cards(search))
    if not card_ids:
        return _fail(EMPTY_MESSAGE)

    filtered_name = _allocate_filtered_name(col, deck_name)
    return _open_preview_deck(col, source_deck_id, filtered_name, search, card_ids)


def start_struggle_review(
    col: Collection, source_deck_id: int, card_ids: Iterable[int]
) -> StartResult:
    """Open a preview filtered deck containing these card ids.

    ``reschedule`` is false, the same path as Free Review. Anki sets
    ``odid`` to each card's home deck while it is in the practice deck, so a
    home/``odid`` allowlist in another add-on still sees these cards.
    Suspended, buried, and new cards are not gathered. An empty id list does
    not create a deck.
    """

    source_deck_id = int(source_deck_id)
    error = _source_error(col, source_deck_id)
    if error:
        return error
    ids = sorted({int(cid) for cid in card_ids})
    if not ids:
        return _fail(NO_STRUGGLE_CARDS_MESSAGE)

    cleanup_all_free_review_sessions(col)
    search = struggle_card_search(col, ids)
    matched = list(col.find_cards(search))
    if not matched:
        return _fail(NO_STRUGGLE_CARDS_MESSAGE)
    deck_name = col.decks.name(DeckId(source_deck_id))
    filtered_name = _allocate_filtered_name(col, deck_name, suffix=STRUGGLE_NAME_SUFFIX)
    return _open_preview_deck(col, source_deck_id, filtered_name, search, matched)


def struggle_card_search(col: Collection, card_ids: Iterable[int]) -> str:
    """Search that gathers these ids and still looks like a Free Review deck.

    The ``-is:new`` / ``-is:suspended`` / ``-is:buried`` markers are what
    cleanup uses to recognize the practice deck. They also match the cards
    a filtered deck is able to hold.
    """

    ids = sorted({int(cid) for cid in card_ids})
    if not ids:
        raise ValueError("struggle search needs at least one card id")
    id_search = "cid:" + ",".join(str(cid) for cid in ids)
    return col.build_search_string(
        id_search,
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_NEW)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_SUSPENDED)),
        SearchNode(negated=SearchNode(card_state=SearchNode.CARD_STATE_BURIED)),
    )


def cleanup_free_review_deck(col: Collection, deck_id: int) -> bool:
    """Return cards home, delete one practice deck we recorded, and patch side effects."""

    deck_id = int(deck_id)
    data = _load_store(col)
    if not data.trusted:
        return False
    keys = [
        key
        for key, session in data.sessions.items()
        if session.get("filtered_deck_id") == deck_id or key == _deck_name_or_none(col, deck_id)
    ]
    if not keys:
        return False
    _finish_sessions(col, data, keys)
    return True


def cleanup_all_free_review_sessions(col: Collection) -> int:
    """Remove practice decks recorded in the restore file and patch side effects.

    A filtered deck is removed only when its id (or, if the id was not saved
    yet, its exact name) is in that file. A user deck that happens to use the
    same name and search is left alone.

    Cards that have since received a real review (reps, interval, ease,
    lapses, or type changed) are left alone. Suspend, bury, and deck moves
    are also left alone.
    """

    data = _load_store(col)
    if not data.trusted:
        return 0
    if not data.sessions:
        return 0
    return _finish_sessions(col, data, list(data.sessions))


def is_free_review_deck(col: Collection, deck_id: int) -> bool:
    return _is_our_deck(col, int(deck_id))


def _source_error(col: Collection, source_deck_id: int) -> StartResult | None:
    if not col.v3_scheduler():
        return _fail(V3_MESSAGE)
    deck_id = DeckId(int(source_deck_id))
    if not col.decks.have(deck_id):
        return _fail(MISSING_DECK_MESSAGE)
    if col.decks.is_filtered(deck_id):
        return _fail(FILTERED_SOURCE_MESSAGE)
    if not _load_store(col).trusted:
        return _fail(SNAPSHOT_CORRUPT_MESSAGE)
    return None


def _open_preview_deck(
    col: Collection,
    source_deck_id: int,
    filtered_name: str,
    search: str,
    card_ids: list[int],
) -> StartResult:
    snapshots = {int(cid): _snapshot_card(col.get_card(cid)) for cid in card_ids}
    # Recorded before the build so cleanup can merge the build, the
    # answers, and the removal into one undo step.
    undo_id = int(col.add_custom_undo_entry("Free Review"))
    _store_session(
        col,
        filtered_name,
        source_deck_id=source_deck_id,
        filtered_deck_id=None,
        snapshots=snapshots,
        undo_id=undo_id,
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
            _filtered_update(0, filtered_name, config)
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
        undo_id=undo_id,
    )
    if not _is_our_deck(col, filtered_id):
        # The build did not land on the preview deck we asked for.
        cleanup_free_review_deck(col, filtered_id)
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


def _omit_mature(scope: str) -> bool:
    if scope == SCOPE_ALL_SEEN:
        return False
    if scope == SCOPE_OMIT_MATURE:
        return True
    raise ValueError(f"unknown free-review scope: {scope}")


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


def _allocate_filtered_name(
    col: Collection, source_name: str, *, suffix: str = ""
) -> str:
    """Pick a deck name that is not already used.

    An existing filtered deck is never reused, even when its name and search
    look like one of ours. Only ids recorded in the restore file are deleted.
    """

    base = f"{FREE_REVIEW_PREFIX}{_display_deck_name(source_name)}{suffix}"
    for index in range(1, 50):
        name = base if index == 1 else f"{base} ({index})"
        if col.decks.id_for_name(name) is None:
            return name
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
    # decay is Anki 25.7+. last_review_time is Anki 25.9.2+. Missing
    # attributes are skipped so an older collection does not raise here.
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
        decay=getattr(card, "decay", None),
        desired_retention=getattr(card, "desired_retention", None),
        last_review_time=getattr(card, "last_review_time", None),
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


def _memory_pair(card) -> tuple[float, float] | None:
    memory = card.memory_state
    if memory is None:
        return None
    return (memory.stability, memory.difficulty)


def _saved_memory(snap: CardSnapshot) -> tuple[float, float] | None:
    if snap.stability is None or snap.difficulty is None:
        return None
    return (snap.stability, snap.difficulty)


def _memory_drifted(card, snap: CardSnapshot) -> bool:
    return _memory_pair(card) != _saved_memory(snap)


def _apply_memory(card, snap: CardSnapshot) -> None:
    saved = _saved_memory(snap)
    if saved is None:
        card.memory_state = None
        return
    card.memory_state = FSRSMemoryState(stability=saved[0], difficulty=saved[1])


def _preview_due_leaked(card, snap: CardSnapshot) -> bool:
    """True when Anki left a preview timestamp in place of due 0."""

    return snap.due == 0 and int(card.due) >= _PREVIEW_DUE_TIMESTAMP


def _restore_cards(col: Collection, cards: dict) -> None:
    """Patch preview side effects only.

    Queue is never written, so a suspend or bury done during the session
    stays. ``did`` is never written, so a browser deck move stays. Due is
    written only for the due==0 timestamp leak. Decay is written only when
    the card object has that field and the value drifted.
    """

    dirty = []
    for cid_text, payload in cards.items():
        try:
            card = col.get_card(int(cid_text))
        except (NotFoundError, TypeError, ValueError):
            continue
        try:
            snap = CardSnapshot(**payload)
        except TypeError:
            logger.warning("skipping unreadable free-review snapshot for card %s", cid_text)
            continue
        if not _unchanged_by_real_review(card, snap):
            continue
        changed = False
        if _preview_due_leaked(card, snap):
            card.due = snap.due
            changed = True
        if hasattr(card, "decay") and card.decay != snap.decay:
            card.decay = snap.decay
            changed = True
        if _memory_drifted(card, snap):
            _apply_memory(card, snap)
            changed = True
        if changed:
            dirty.append(card)
    if dirty:
        col.update_cards(dirty)


@dataclass
class _Store:
    sessions: dict
    trusted: bool


def _finish_sessions(col: Collection, data: _Store, keys: list[str]) -> int:
    """Delete the recorded practice decks, patch side effects, collapse undo."""

    removed = 0
    sessions = [data.sessions[key] for key in keys if key in data.sessions]
    for key in keys:
        session = data.sessions.get(key)
        if session is None:
            continue
        deck_id = _session_deck_id(col, key, session)
        if deck_id is not None and col.decks.have(DeckId(deck_id)) and col.decks.is_filtered(
            DeckId(deck_id)
        ):
            col.decks.remove([DeckId(deck_id)])
            removed += 1
    for session in sessions:
        _restore_cards(col, session.get("cards") or {})
    for session in sessions:
        _collapse_undo(col, session.get("undo_id"))
    for key in keys:
        data.sessions.pop(key, None)
    _write_store(col, data)
    return removed


def _session_deck_id(col: Collection, name: str, session: dict) -> int | None:
    recorded = session.get("filtered_deck_id")
    if recorded:
        return int(recorded)
    # The deck was created before its id was saved. Remove that exact name
    # only when it is one of our preview decks.
    existing = col.decks.id_for_name(name)
    if existing is not None and _is_our_deck(col, int(existing)):
        return int(existing)
    return None


def _collapse_undo(col: Collection, undo_id: int | None) -> None:
    """Fold the build, answers, and removal into the session's undo step.

    One Undo then returns to the moment before Free Review, instead of
    bringing the temporary deck back or undoing only the due/decay patch.
    A stale id (Anki was restarted) is ignored.
    """

    if not undo_id:
        return
    try:
        col.merge_undo_entries(int(undo_id))
    except InvalidInput:
        logger.info("free-review undo id %s is no longer on the stack", undo_id)


def _deck_name_or_none(col: Collection, deck_id: int) -> str | None:
    if not col.decks.have(DeckId(deck_id)):
        return None
    return col.decks.name(DeckId(deck_id))


def _store_session(
    col: Collection,
    name: str,
    *,
    source_deck_id: int,
    filtered_deck_id: int | None,
    snapshots: dict[int, CardSnapshot],
    undo_id: int | None,
) -> None:
    data = _load_store(col)
    if not data.trusted:
        return
    data.sessions[name] = {
        "source_deck_id": source_deck_id,
        "filtered_deck_id": filtered_deck_id,
        "undo_id": undo_id,
        "cards": {str(cid): asdict(snap) for cid, snap in snapshots.items()},
    }
    _write_store(col, data)


def _drop_session(col: Collection, name: str) -> None:
    data = _load_store(col)
    if not data.trusted:
        return
    if name in data.sessions:
        del data.sessions[name]
        _write_store(col, data)


def _load_store(col: Collection) -> _Store:
    path = snapshot_path(col)
    if not os.path.exists(path):
        return _Store(sessions={}, trusted=True)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _warn_corrupt(path, exc)
        return _Store(sessions={}, trusted=False)
    if not isinstance(data, dict) or data.get("version") != _SNAPSHOT_VERSION:
        _warn_corrupt(path, "unexpected restore-file version")
        return _Store(sessions={}, trusted=False)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        _warn_corrupt(path, "restore file has no session map")
        return _Store(sessions={}, trusted=False)
    return _Store(sessions=sessions, trusted=True)


def _warn_corrupt(path: str, reason: object) -> None:
    message = f"{SNAPSHOT_CORRUPT_MESSAGE} The file is still here: {path}"
    if message in _snapshot_warnings:
        return
    logger.warning("%s (%s)", message, reason)
    _snapshot_warnings.append(message)


def _write_store(col: Collection, data: _Store) -> None:
    path = snapshot_path(col)
    if not data.sessions:
        if os.path.exists(path):
            os.remove(path)
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        return
    payload = {"version": _SNAPSHOT_VERSION, "sessions": data.sessions}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(tmp, path)
