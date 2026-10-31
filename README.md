# Free Review

Anki addon that starts a practice session of **every seen card** in a deck and its subdecks. New cards stay out. Again, Hard, Good, and Easy do not change due dates, intervals, ease, reps, or lapses. Optional struggle tracking is off until you turn it on.

This is the same idea as Custom Study / a filtered deck with **Reschedule cards based on my answers in this deck** turned off.

## What it does

On the deck overview, click **Free Review** (shortcut **F**). Or open the gear menu in the deck list and choose **Free Review**. A small dialog asks which cards to practice. Cancel closes it and does not create a deck.

- **All seen cards** — learning, young, and mature.
- **Omit mature cards** — learning and young only.
- **Track struggle and session stats** — off by default. Anki remembers the last choice (`track_struggle` in the add-on config). Leave it off and Free Review ends the way it always has: no running total and no summary.

A card is mature when its interval is **at least 21 days** (`prop:ivl>=21`). That is the cutoff Anki's browser uses. Anki 25.9.2 has no `is:mature` search term. Learning cards usually have an interval of 0, so they stay. A relearning card keeps its previous interval, so a lapsed mature card is omitted too.

Anki then builds a temporary filtered deck named `Free Review - <deck>` (parent `::` names are shown as ` / `, so the practice deck is not nested). Only cards that have already been seen are gathered:

| Included | Excluded |
| --- | --- |
| Review | New / unseen |
| Learning | Suspended |
| Relearning | Buried (sibling and manual) |
| The selected deck and its subdecks, not only cards that are due | Cards already sitting in another filtered deck |

Buried and suspended cards are excluded on purpose. Anki's filtered-deck builder will not gather them even if the search asks for them. That matches the exclusion list in the filtered deck options dialog.

Cards are ordered at random. Again shows the card again after 1 minute and Hard after 10 minutes, inside this session only. Good and Easy return the card to its home deck. Those delays are the ones Anki's own non-rescheduling custom study uses. They are not written onto the home-deck schedule.

If the deck has no matching cards (nothing seen, or only mature cards when those are omitted), Anki shows "No seen cards to practice in this deck." and does not create a deck.

When you leave the reviewer (finish, Back, Decks), quit, or sync, the temporary deck is deleted and cards are back in the original decks. The same cleanup runs on profile open, before automatic sync, so a crash does not leave `Free Review - ...` decks behind or upload them.

## Struggle tracking

Turn on **Track struggle and session stats** in the Free Review dialog when you want a tally for that run. The checkbox starts off. The last choice is saved in the add-on config, so the next dialog opens the way you left it. Cancel does not change the saved choice.

While the session is open, a line between the card and the answer buttons shows:

`Free Review: Answered 4 · Solid 3 · Struggle 1 · 1:05`

- **Answered** counts every Again, Hard, Good, and Easy press. One card can add more than one.
- **Solid** is Good or Easy.
- **Struggle** is Again or Hard.
- The clock is time since this session started. While the running line is visible it advances about once a second. Showing or answering a card still refreshes the answered, solid, and struggle counts right away. The clock stops when you leave the reviewer, when the summary opens, and when quit or sync clears the session. The summary uses the time until you leave the reviewer. A struggle-card revisit uses the same running line, so its clock ticks the same way.

**Cards seen** (in the summary) is the number of distinct cards shown or answered.

**Struggle cards** follow one rule: the first Again or Hard puts that card on the list for the rest of the session, including the summary. A later Good or Easy does not take it off. The list is the cards you had to come back to, not whichever button you pressed last.

The totals stay in memory for that run. They are not written into the restore file, and they do not change cleanup or scheduling. Undo during the session puts the card back and takes that answer out of the tally: answered, solid, struggle, and the struggle set. Redo puts the same answer back. Several Undos peel answers off in reverse order, and Redos put them back in that order. The clock does not rewind. The summary counts the answers that are still standing when you leave.

When you leave the reviewer, a **Free Review summary** dialog shows:

- time spent
- cards seen
- answered, solid, and struggle
- success rate, which is solid answers ÷ answered (`n/a` when you did not answer)
- how many struggle cards, and the rule above

**Close** dismisses it. If any card was struggled, two more buttons are there:

- **Review struggle cards** starts another Free Review filtered deck of those card ids only, with struggle tracking still on. `reschedule` is still false, so due dates, intervals, ease, reps, and lapses stay put. The deck is named `Free Review - <deck> (struggle)`. Cards still move through Anki's filtered deck, with `odid` set to the home deck for the length of that session. Suspended, buried, and new cards are skipped, same as a normal Free Review. If none of the struggle cards can be gathered, Anki shows "No struggle cards to practice." and does not create a deck.
- **Show in Browser** opens the card browser on `cid:…` for every struggle id, including cards the filtered deck cannot gather. That button leaves the summary open, so you can still review them afterward.

Quit and sync still delete the practice deck. They do not open the summary, because a dialog there would block shutdown or sync. With tracking off, leaving the reviewer does not open a summary either.

## Scheduling and AnkiWeb

The practice deck is created with Anki's supported API, `add_or_update_filtered_deck`, and **`reschedule` is false**. That is preview mode (`SchedulingState.Filtered.Preview`), not a rescheduling filtered deck.

What that means, checked against the Anki **25.9.2** library:

- The scheduler does not run the normal review transition. Reps are not incremented. Interval, ease, lapses, card type, and FSRS stability/difficulty are not rewritten by the answer.
- Each answer appends a revlog row of type **cram / filtered** (`REVLOG_CRAM`, type 3), with ease 1–4. That row is ordinary review history: it **syncs to AnkiWeb** with the rest of the revlog, the same way a built-in non-rescheduling filtered deck does. It is not a new interval. Anki's FSRS memory-state path skips cram revlogs, and the next intervals reported by `get_scheduling_states` are unchanged after the session. A future scheduler that stopped ignoring type 3 would start treating these rows as real reviews; this addon does not delete them, because that is not what Anki's own preview mode does.
- The card row's modification time and USN change, so the card is part of the next sync. After cleanup, the synced scheduling columns match the pre-session values.
- While the session is open, cards live in the temporary deck (`did` / `odid` / `odue`, and `due` / `queue` for Again and Hard). Cleanup puts them back **before** auto-sync on quit (`profile_will_close` runs first) and **before** auto-sync on the next open (`profile_did_open` runs first). Starting a sync also ends the session first. Killing Anki and copying the collection to another machine without opening this profile can still upload the temporary deck; open the profile once so cleanup runs before you sync.

Two Anki preview side effects are patched after the temporary deck is removed. Nothing else is written back:

1. **Due value 0.** When Anki returns a card from a filtered deck it restores `due` only if the saved original due is non-zero. Again or Hard on a card due on day 0 leaves a unix timestamp in `due`. If the saved due was 0 and the current due is a timestamp, it is set back to 0. A deliberate Set Due Date (a day number, not a timestamp) is not overwritten.
2. **FSRS memory.** With FSRS on, a preview answer stores the current FSRS decay (0.154 in 25.9). A relearning card that had no memory state also gained stability and difficulty on Easy in 25.9.2. Decay, stability, and difficulty are written back to the pre-session values. `Card.decay` exists on Anki 25.7+; this addon requires 25.9.2, where `Card.last_review_time` also exists. On an older build those attributes are read with `getattr` and simply skipped.

Suspend, bury, and moving the card to another deck during the session are kept. The restore does not write `queue` or `did`. It also does nothing when reps, interval, ease, lapses, or type changed, so a later real review is not rolled back.

On Anki 25.9.2, answering in this preview deck did **not** sibling-bury the other card on the note (checked for a new sibling, and for a second review card that was also in the practice deck). If a future Anki version does bury siblings, that bury is left in place.

While the session is still open, each answer is its own Undo step. Undo puts the card back and takes that answer out of the struggle tally. Redo puts the same answer back on the tally. When you leave, cleanup folds the build, the answers still standing, the removal, and the due/decay patch into one Undo step named Free Review. One Undo then returns to the moment before Free Review. It does not leave the temporary deck behind or a timestamp in `due`. If Anki was killed mid-session, the cleanup on next launch is a new undo step, because the old undo stack is gone. Undoing that step can bring the practice deck back; don't undo it, or restart so cleanup can run again.

The addon does not call a rescheduling answer path and does not set `reschedule` to true. Struggle tracking only counts answers. It does not write card fields, and the struggle revisit deck uses this same preview path.

## Anki versions

Tested with the `anki` **25.09.2** Python library (`int_version()` is **250902**). `manifest.json` sets `min_point_version` to that value, so Anki marks the addon incompatible on anything older. `Card.decay` arrived in 25.7 and `Card.last_review_time` in 25.9.2; snapshotting those fields on 23.10–25.7 raised `AttributeError` after a session had already started. The desktop UI still needs the V3 scheduler, which this version has. Anki 23.10 is not supported.

## Install

From a file. Anki reads `manifest.json` from the **root** of the zip and extracts those files into `addons21/anki_free_review/`:

```bash
cd anki_free_review
zip -r ../free-review.ankiaddon manifest.json __init__.py session.py tracking.py ui.py config.json
```

In Anki: **Tools → Add-ons → Install from file…** and choose `free-review.ankiaddon`. Restart Anki. Requires Anki 25.9.2 or newer (`min_point_version` 250902).

From a checkout, symlink the addon folder into Anki's add-on directory (then restart):

```bash
# macOS
ln -s "$PWD/anki_free_review" "$HOME/Library/Application Support/Anki2/addons21/anki_free_review"

# Linux
ln -s "$PWD/anki_free_review" ~/.local/share/Anki2/addons21/anki_free_review
```

No config is required. **Track struggle and session stats** stays off until you turn it on. Anki then remembers that checkbox in the add-on config (`track_struggle`). Tools → Add-ons → Free Review → Config edits the same value.

## Uninstall

1. Leave the reviewer or quit Anki once while the addon is still installed, so it can delete `Free Review - ...` and apply the local restore file.
2. **Tools → Add-ons**, select **Free Review**, Delete, restart.

The restore file is `collection.anki2.free-review.json` next to your collection. AnkiWeb does not sync it. It is removed after a successful cleanup. If the file is corrupt, Free Review leaves it in place, shows a warning, and does not delete decks or start a new session. Move the file aside if you want to keep going without those saved card states. If you delete the addon while a practice deck is still listed, delete that filtered deck in the deck list: Anki returns those cards to their home decks. Day-0 due values are the case the restore file covers; open Anki once with the addon installed before removing it if a session was interrupted.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The suite builds a temporary collection and does not touch a real profile. It checks:

- Seen cards in a parent deck, subdeck, and grandchild are gathered; new, suspended, buried, and other-deck cards are not.
- All seen cards include interval ≥ 21. Omit mature (`-prop:ivl>=21`) keeps learning and young cards (interval 20 and interval 0) and leaves mature cards out. Answers plus cleanup still do not change scheduling fields. A deck of only mature cards shows the empty message and creates no deck. Cancelling the dialog creates nothing. The struggle-tracking checkbox is independent of All seen / Omit mature, and cancel does not change the saved checkbox.
- Good and Easy count as solid. Again and Hard count as struggle. A card that gets Again or Hard stays in the struggle set after a later Good or Easy. Summary totals match that sequence. The running line is `Free Review: Answered N · Solid N · Struggle N · M:SS`. While that line is up, a one-second timer rewrites the clock and stops when the line is hidden or the label is gone. The tick does not record answers.
- A tracked session plus cleanup, and a follow-up struggle-card session plus cleanup, leave scheduling fields unchanged. While the struggle deck is open, `odid` is the home deck and `reschedule` is false. The restore file has no struggle store. The browser search is `cid:…`.
- Tracking off does not create struggle totals and does not require a summary. An empty struggle-id list creates no deck. Suspended and new cards are left out of the struggle deck and still match the browser `cid:` search. Undo after a struggle session does not bring the practice deck back.
- The filtered deck is created with `reschedule` false, and the current scheduling state is preview, not a rescheduling filter.
- Again, Hard, Good, and Easy, including Again left in progress, leave due, interval, ease, reps, lapses, type, queue, odue, odid, deck id, FSRS stability/difficulty, decay, and the next intervals from `get_scheduling_states` unchanged. A due of 0 stays 0.
- Suspend, bury, a browser deck move, and Set Due Date done during the session are still there after cleanup.
- Undo after an answer rolls answered, solid, struggle, and the struggle set back to the pre-answer tally. Redo puts that answer back. Several undos and redos follow stack order, including a card that stays on the struggle list after a later Good. The running line shows the rolled-back counts, and its clock does not rewind. Scheduling fields stay unchanged. A new answer discards grades waiting to be redone.
- Undo after a normal session end does not resurrect the practice deck or put a timestamp back in `due`. The same holds after a session whose answers were undone and redone.
- A filtered deck that merely matches the Free Review name and search is not deleted.
- A corrupt restore file is left in place and produces a warning, instead of dropping the saved card states.
- Revlog rows written by those answers are only type cram.
- The temporary deck and the restore file are gone, and home-deck card counts match.
- An empty deck and a new-only deck do not create a deck.
- A user deck already named `Free Review - ...` is not deleted or overwritten.
- A snapshot is not applied to a card that has since had a real review.
- A normal Good answer (the control) does change reps and the interval, so the comparisons above are not vacuous.

## License

MIT. See [LICENSE](LICENSE).
