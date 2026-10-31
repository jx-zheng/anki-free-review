"""In-memory struggle tracking for one Free Review run.

Nothing in this module writes the collection or the restore file. Scheduling
stays on the filtered-deck preview path.

Struggle-set rule: the first Again (1) or Hard (2) puts that card id in
``struggle_card_ids`` for the rest of the session. A later Good (3) or Easy
(4) does not remove it. Undo of that Again or Hard takes the card off the set
when no earlier struggle answer for it is still standing. ``solid`` and
``struggle`` count answers, not unique cards. The success rate is solid
answers divided by answers. ``seen_card_ids`` is every card shown or answered.

Graded answers sit on a stack. Undo pops the latest answer's counts, and its
struggle-set membership when that answer added the card. Redo pushes the same
answer back. A new answer clears the redo stack, same as Anki dropping its
redo queue when the next grade is given. The elapsed clock is not rewound.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

EASE_AGAIN = 1
EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4
SOLID_EASES = frozenset({EASE_GOOD, EASE_EASY})
STRUGGLE_EASES = frozenset({EASE_AGAIN, EASE_HARD})

TRACK_STRUGGLE_CONFIG_KEY = "track_struggle"

SUMMARY_REVIEW = "review"
SUMMARY_BROWSER = "browser"
SUMMARY_CLOSE = "close"

STRUGGLE_SET_RULE = (
    "A card stays on the struggle list after Again or Hard, "
    "even if a later answer is Good or Easy."
)


def classify_ease(ease: int) -> str | None:
    """Return ``solid``, ``struggle``, or None when ``ease`` is not an answer."""

    try:
        value = int(ease)
    except (TypeError, ValueError):
        return None
    if value in SOLID_EASES:
        return "solid"
    if value in STRUGGLE_EASES:
        return "struggle"
    return None


@dataclass
class _AnswerRecord:
    """One graded answer, and how to put it back after undo.

    ``undo_step`` is Anki's undo counter for this answer as it currently
    sits on the undo stack. Undo and redo each build a new step, so the
    counter is replaced with ``redo_step`` when the answer is undone and
    with the new undo counter when it is redone. ``added_seen`` and
    ``added_struggle`` are the set memberships this answer introduced, so
    undo can drop them and redo can restore them without touching an
    earlier show or an earlier Again/Hard.
    """

    card_id: int
    kind: str
    added_seen: bool
    added_struggle: bool
    undo_step: int | None = None
    redo_step: int | None = None


@dataclass
class SessionStats:
    """Answer totals for a single tracked Free Review run."""

    started_at: float
    ended_at: float | None = None
    answered: int = 0
    solid: int = 0
    struggle: int = 0
    seen_card_ids: set[int] = field(default_factory=set)
    struggle_card_ids: set[int] = field(default_factory=set)
    _answers: list[_AnswerRecord] = field(default_factory=list, repr=False)
    _redo: list[_AnswerRecord] = field(default_factory=list, repr=False)

    def note_seen(self, card_id: int) -> None:
        self.seen_card_ids.add(int(card_id))

    def record_answer(
        self, card_id: int, ease: int, *, undo_step: int | None = None
    ) -> str | None:
        """Count one answer. Again/Hard joins the struggle set and stays there.

        ``undo_step`` is the collection undo counter after this grade. Undo
        of a different step leaves the tally alone. A grade clears answers
        waiting to be redone.
        """

        kind = classify_ease(ease)
        if kind is None:
            return None
        card_id = int(card_id)
        self._redo.clear()
        entry = _AnswerRecord(
            card_id=card_id,
            kind=kind,
            added_seen=card_id not in self.seen_card_ids,
            added_struggle=(
                kind == "struggle" and card_id not in self.struggle_card_ids
            ),
            undo_step=None if undo_step is None else int(undo_step),
        )
        self._answers.append(entry)
        self._apply(entry)
        return kind

    def undo_answer(self, undone_step: int, redo_step: int) -> bool:
        """Pop the latest answer when ``undone_step`` is its undo counter.

        False leaves the tally unchanged: this undo is not the latest grade.
        """

        if not self._answers:
            return False
        entry = self._answers[-1]
        if entry.undo_step != int(undone_step):
            return False
        self._answers.pop()
        entry.redo_step = int(redo_step)
        self._redo.append(entry)
        self._unapply(entry)
        return True

    def redo_answer(self, redone_step: int, undo_step: int) -> bool:
        """Re-apply the latest undone answer when ``redone_step`` matches it.

        ``undo_step`` is the counter Anki assigned when it put the grade
        back on the undo stack, so a later Undo finds this same answer.
        """

        if not self._redo:
            return False
        entry = self._redo[-1]
        if entry.redo_step != int(redone_step):
            return False
        self._redo.pop()
        entry.undo_step = int(undo_step)
        self._answers.append(entry)
        self._apply(entry)
        return True

    def _apply(self, entry: _AnswerRecord) -> None:
        self.seen_card_ids.add(entry.card_id)
        self.answered += 1
        if entry.kind == "solid":
            self.solid += 1
            return
        self.struggle += 1
        if entry.added_struggle:
            self.struggle_card_ids.add(entry.card_id)

    def _unapply(self, entry: _AnswerRecord) -> None:
        self.answered -= 1
        if entry.kind == "solid":
            self.solid -= 1
        else:
            self.struggle -= 1
            if entry.added_struggle:
                self.struggle_card_ids.discard(entry.card_id)
        if entry.added_seen:
            self.seen_card_ids.discard(entry.card_id)

    def freeze(self, *, now: float) -> None:
        """Stop the elapsed clock at the moment the session ends."""

        if self.ended_at is None:
            self.ended_at = now

    def elapsed_seconds(self, *, now: float) -> float:
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, end - self.started_at)


def begin_session_tracking(enabled: bool, *, now: float | None = None) -> SessionStats | None:
    """Start totals when the user opted in. Off returns None and stores nothing."""

    if not enabled:
        return None
    if now is None:
        now = time.monotonic()
    return SessionStats(started_at=now)


def note_card_seen(
    stats: SessionStats | None, *, in_tracked_deck: bool, card_id: int
) -> bool:
    """Record a shown card. False when tracking is off or the deck is not ours."""

    if stats is None or not in_tracked_deck:
        return False
    stats.note_seen(card_id)
    return True


def accept_review_answer(
    stats: SessionStats | None,
    *,
    in_tracked_deck: bool,
    card_id: int,
    ease: int,
    undo_step: int | None = None,
) -> str | None:
    """Count an answer when the reviewer is still on the practice deck.

    The caller decides that. Good and Easy return the card home before the
    reviewer hook runs, so ``card.did`` is not the practice deck.
    ``undo_step`` is the collection undo counter of this grade.
    """

    if stats is None or not in_tracked_deck:
        return None
    return stats.record_answer(card_id, ease, undo_step=undo_step)


def undo_review_answer(
    stats: SessionStats | None, *, undone_step: int, redo_step: int
) -> bool:
    """Roll back one graded answer. False when this undo is not that grade."""

    if stats is None:
        return False
    return stats.undo_answer(undone_step, redo_step)


def redo_review_answer(
    stats: SessionStats | None, *, redone_step: int, undo_step: int
) -> bool:
    """Put back one undone grade. False when this redo is not that grade."""

    if stats is None:
        return False
    return stats.redo_answer(redone_step, undo_step)


def should_show_summary(stats: SessionStats | None) -> bool:
    """A summary exists only for a run that opted in. Off keeps the old ending."""

    return stats is not None


def tracking_enabled_from_config(config: dict | None) -> bool:
    """Default off unless the saved add-on config says true."""

    if not isinstance(config, dict):
        return False
    return config.get(TRACK_STRUGGLE_CONFIG_KEY, False) is True


def config_with_tracking(config: dict | None, enabled: bool) -> dict:
    """Copy ``config`` and store the checkbox value. Other keys are kept."""

    updated = dict(config or {})
    updated[TRACK_STRUGGLE_CONFIG_KEY] = bool(enabled)
    return updated


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_success_rate(solid: int, answered: int) -> str:
    """Solid answers / answers, as a percent. No answers is ``n/a``."""

    if answered <= 0:
        return "n/a"
    percent = round(100.0 * solid / answered, 1)
    if percent == int(percent):
        return f"{int(percent)}%"
    return f"{percent:.1f}%"


def format_running_stats(stats: SessionStats, *, now: float) -> str:
    """One line for the reviewer bottom bar."""

    elapsed = format_elapsed(stats.elapsed_seconds(now=now))
    return (
        f"Free Review: Answered {stats.answered} · "
        f"Solid {stats.solid} · Struggle {stats.struggle} · {elapsed}"
    )


def format_summary_text(stats: SessionStats, *, ended_at: float | None = None) -> str:
    """Body of the end-of-session dialog."""

    if ended_at is None:
        ended_at = stats.ended_at if stats.ended_at is not None else stats.started_at
    rate = format_success_rate(stats.solid, stats.answered)
    lines = [
        f"Time spent: {format_elapsed(ended_at - stats.started_at)}",
        f"Cards seen: {len(stats.seen_card_ids)}",
        f"Answered: {stats.answered}",
        f"Solid (Good or Easy): {stats.solid}",
        f"Struggle (Again or Hard): {stats.struggle}",
        f"Success rate: {rate} (solid / answered)",
        f"Struggle cards: {len(stats.struggle_card_ids)}",
        "",
        STRUGGLE_SET_RULE,
    ]
    return "\n".join(lines)


def struggle_browser_search(card_ids: Iterable[int]) -> str:
    """Browser search for the struggle set. Empty when there is nothing to show."""

    ids = sorted({int(cid) for cid in card_ids})
    if not ids:
        return ""
    return "cid:" + ",".join(str(cid) for cid in ids)


def summary_actions(stats: SessionStats, *, source_deck_id: int | None) -> tuple[str, ...]:
    """Buttons for the summary. Close is always available.

    Review needs a source deck so the new filtered deck has somewhere to
    return cards. Browser only needs the card ids.
    """

    actions: list[str] = []
    if stats.struggle_card_ids and source_deck_id:
        actions.append(SUMMARY_REVIEW)
    if stats.struggle_card_ids:
        actions.append(SUMMARY_BROWSER)
    actions.append(SUMMARY_CLOSE)
    return tuple(actions)
