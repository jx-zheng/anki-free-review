# Free Review

Anki addon that starts a practice session of **every seen card** in a deck and its subdecks. New cards stay out. Again, Hard, Good, and Easy do not change due dates, intervals, ease, reps, or lapses.

This is the same idea as Custom Study / a filtered deck with **Reschedule cards based on my answers in this deck** turned off.

## What it does

On the deck overview, click **Free Review** (shortcut **F**). Or open the gear menu in the deck list and choose **Free Review**.

Anki builds a temporary filtered deck named `Free Review - <deck>` (parent `::` names are shown as ` / `, so the practice deck is not nested). Only cards that have already been seen are gathered:

| Included | Excluded |
| --- | --- |
| Review | New / unseen |
| Learning | Suspended |
| Relearning | Buried (sibling and manual) |
| The selected deck and its subdecks, not only cards that are due | Cards already sitting in another filtered deck |

Buried and suspended cards are excluded on purpose. Anki's filtered-deck builder will not gather them even if the search asks for them. That matches the exclusion list in the filtered deck options dialog.

Cards are ordered at random. Again shows the card again after 1 minute and Hard after 10 minutes, inside this session only. Good and Easy return the card to its home deck. Those delays are the ones Anki's own non-rescheduling custom study uses. They are not written onto the home-deck schedule.

If the deck has no seen cards, Anki shows "No seen cards to practice in this deck." and does not create a deck.

When you leave the reviewer (finish, Back, Decks), quit, or sync, the temporary deck is deleted and cards are back in the original decks. The same cleanup runs on profile open, before automatic sync, so a crash does not leave `Free Review - ...` decks behind or upload them.

## Scheduling and AnkiWeb

The practice deck is created with Anki's supported API, `add_or_update_filtered_deck`, and **`reschedule` is false**. That is preview mode (`SchedulingState.Filtered.Preview`), not a rescheduling filtered deck.

What that means, checked against the Anki **25.9.2** library:

- The scheduler does not run the normal review transition. Reps are not incremented. Interval, ease, lapses, card type, and FSRS stability/difficulty are not rewritten by the answer.
- Each answer appends a revlog row of type **cram / filtered** (`REVLOG_CRAM`, type 3), with ease 1–4. Anki's FSRS memory-state path ignores cram revlogs. These rows **do sync to AnkiWeb**, the same way a built-in non-rescheduling filtered deck does. They are review history of type Filtered, not a new interval.
- The card row's modification time and USN change, so the card is part of the next sync. After cleanup, the synced scheduling columns match the pre-session values.
- While the session is open, cards live in the temporary deck (`did` / `odid` / `odue`, and `due` / `queue` for Again and Hard). Cleanup puts them back **before** auto-sync on quit (`profile_will_close` runs first) and **before** auto-sync on the next open (`profile_did_open` runs first). Starting a sync also ends the session first. Killing Anki and copying the collection to another machine without opening this profile can still upload the temporary deck; open the profile once so cleanup runs before you sync.

Two Anki behaviors are corrected locally so the card matches the snapshot. Both are covered by tests:

1. **Due value 0.** When Anki returns a card from a filtered deck it restores `due` only if the saved original due is non-zero. A review or learning card due on day 0 therefore keeps the preview timestamp after Again or Hard. The addon snapshots scheduling fields before the gather and writes them back after the temporary deck is removed.
2. **FSRS decay.** With FSRS on, a preview answer stores the current FSRS decay (0.154 in 25.9) on the card even when stability and difficulty do not change. The addon writes the previous decay back. Measured next intervals were already the same; restoring decay keeps the stored parameter the same too.

Restore is skipped for a card whose reps, interval, ease, lapses, or type changed after the snapshot. A real review touches those, so a leftover snapshot cannot roll back a later real review.

The addon does not call a rescheduling answer path and does not set `reschedule` to true.

## Anki versions

Tested with the `anki` **25.9.2** Python library (V3 scheduler, the current 25.x line). The desktop UI targets **Anki 23.10+** (V3 scheduler and four-button preview). On older schedulers the addon refuses to start and shows a short message.

## Install

From a file. Anki reads `manifest.json` from the **root** of the zip and extracts those files into `addons21/anki_free_review/`:

```bash
cd anki_free_review
zip -r ../free-review.ankiaddon manifest.json __init__.py session.py ui.py
```

In Anki: **Tools → Add-ons → Install from file…** and choose `free-review.ankiaddon`. Restart Anki. `min_point_version` is 231000 (Anki 23.10).

From a checkout, symlink the addon folder into Anki's add-on directory (then restart):

```bash
# macOS
ln -s "$PWD/anki_free_review" ~/Library/Application Support/Anki2/addons21/anki_free_review

# Linux
ln -s "$PWD/anki_free_review" ~/.local/share/Anki2/addons21/anki_free_review
```

No config is required.

## Uninstall

1. Leave the reviewer or quit Anki once while the addon is still installed, so it can delete `Free Review - ...` and apply the local restore file.
2. **Tools → Add-ons**, select **Free Review**, Delete, restart.

The restore file is `collection.anki2.free-review.json` next to your collection. AnkiWeb does not sync it. It is removed after a successful cleanup. If you delete the addon while a practice deck is still listed, delete that filtered deck in the deck list: Anki returns those cards to their home decks. Day-0 due values are the case the restore file covers; open Anki once with the addon installed before removing it if a session was interrupted.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The suite builds a temporary collection and does not touch a real profile. It checks:

- Seen cards in a parent deck, subdeck, and grandchild are gathered; new, suspended, buried, and other-deck cards are not.
- The filtered deck is created with `reschedule` false, and the current scheduling state is preview, not a rescheduling filter.
- Again, Hard, Good, and Easy, including Again left in progress, leave due, interval, ease, reps, lapses, type, queue, odue, odid, deck id, FSRS stability/difficulty, decay, and the next intervals from `get_scheduling_states` unchanged. A due of 0 stays 0.
- Revlog rows written by those answers are only type cram.
- The temporary deck and the restore file are gone, and home-deck card counts match.
- An empty deck and a new-only deck do not create a deck.
- A user deck already named `Free Review - ...` is not deleted or overwritten.
- A snapshot is not applied to a card that has since had a real review.
- A normal Good answer (the control) does change reps and the interval, so the comparisons above are not vacuous.
