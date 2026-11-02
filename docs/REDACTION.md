# Redacting a source

`brain redact` removes one captured source (an email, a Slack message, a document, a meeting transcript) and every
copy of it that the brain derived. It is for a privacy request, or a password pasted into a chat. It cannot be undone.

## Run it

    brain redact SOURCE_ID --reason "privacy request 2026-11-02" --instance ~/Brains/work

Find the id with `brain read --args '{"query":"..."}' search` or `brain read --args '{...}' records_query`. The command prints what it will change and asks
you to type the id. `--yes` skips the question (an agent must pass it). On a hub, run it in the container:
`docker compose exec brain brain redact ...`.

- **Keep the reason free of names, addresses and the text itself.** It is stored in plain text in the tombstone and in
  the write log. The command refuses a reason that repeats 16 or more letters and digits of the source (ignoring case, spaces and punctuation), or holds an `@`. That guard is a net for accidents: a name or a short secret still passes, so keep them out yourself.
- Every version of the item is redacted together, because a changed document keeps its older versions.
- `--backups DIR` (repeatable) names more folders of bundles for it to clean. Each must already exist. If the command stops and you run it again, pass the same folders.

## What it does

1. Replaces the source with a tombstone (`redacted on <date> by <you>: <reason>`). The id, the date and the item's
   name stay, so a later fetch of the same text creates nothing.
2. Scrubs every copy: claims, facts and their history, decisions, records and their history, actions, checklist items,
   projects, signals, thread subjects, meeting titles, held and refused packets, errors and the write log's packets.
   Text values become `[redacted]` (a url becomes `https://redacted.invalid/`). Quotes inside notes become `[redacted]`.
   A fact that rested on the redacted claim is worked out again from the claims left. If an older claim it had superseded
   was still valid, the fact retires; re-verify it.
3. Opens a drift signal on each fact and decision it touched, so someone re-checks what rested on it.
4. Rebuilds the search index, then compacts the file so the removed words are really gone.
5. Deletes the `brain-fetch` export files that hold the item, takes a fresh `post-redaction` backup and deletes the older
   bundles in `<instance>/backups/` and in any `--backups` folder.

## What it leaves

- Numbers, dates and true/false values on claims and facts. If the number itself is the secret, redaction has not
  removed it.
- The item's name: its id, its `external_ref` (a file path or a message id), and the `locator` columns.
- People and system records, and any note or summary that copies the text **without quote marks** (even word for word) or
  restates it. The scrub finds quoted text that the evidence gate proved was in the source. It does not find anything else.
- Short labels an agent typed or copied: a claim's `scope` (a word from the source, up to 40 characters), every `locator`,
  the `intent` of a change, the section and reference of a checklist item, and the id an agent chose for a record.
- A thread whose subject only resembles the item's: a thread is changed when it is linked to the item, or when its
  subject (without `Re:` or `Fwd:`) equals the item's title and has four words or 20 characters. Every thread changed
  by the subject rule is listed in the receipt. A subject with added words is not matched: search for it.
- Hashes (`content_sha256`, `payload_sha256`) and the `key` of each sentence in an unchecked note. They are not text, but a hash can confirm a guess at the removed text.
- Copies of the text that are not in quote marks. The scrub finds quoted text that the evidence gate proved was in the
  source, so a copy in backticks, guillemets or plain words stays. The plan and the receipt list the note rows that
  still hold five or more consecutive words of the removed text, or a bracketed copy of it (`Still holds the text
  without quote marks ... (listed, not removed)`). They report row ids only, and you edit or remove those rows
  yourself.
- Disk blocks the operating system has not reused yet (the rollback journal and the old file that compaction replaced). They belong to no file. Use full-disk encryption (FileVault on a Mac) if that matters.
- Anything outside the folders above. See the table.

## After it runs

- Delete or move **the original** (the file in the documents folder, the email, the chat message). The brain only holds
  copies. If the original stays, `brain-fetch run --rescan` copies it into the inbox again for 14 days.
  The command prints the path when the original is still there.
- A redacted version is not captured again while its text is the item's latest version, and the brain also skips a
  file whose text matches any redacted version. That includes the **clean current version** of a changed document,
  which is redacted with the rest of the chain: to bring it back, change the file (any edit does). Once the file is
  removed, or changed and then changed back to the removed text, the capture leaves it out too. Keep the original out
  of the folder.
- `brain check` should print `{"ok": true, ...}`. It reports a redaction that did not finish, a leftover copy, and any
  change to the engine's triggers.
- If the command stops part-way (disk full, a busy database, a power cut), run the **same command again**. It finishes
  the job. Until it does, `brain check` fails and `brain backup` refuses, because free pages in the file still hold the
  text. A stop during compaction can leave a `brain.sqlite3-journal` file: the same command rolls it back first.
- A live collector (Gmail, Slack, calendar, Vanta, GitHub) reads its system again on every poll. The plan names the
  collector that wrote the item. A redacted thread subject or calendar title stays redacted on the next poll, and
  Vanta stops storing the names of failing tests once a snapshot of that fact was redacted. The original in the
  external system is still yours to change.
- The brain refuses the same text under another id (`this text was redacted (source <id>)`). A copy that differs by
  one character is a different text and is accepted.
- Restoring a bundle made before the redaction brings the text back, and nothing in that older store says it was
  redacted. Keep a note of redacted ids and run `brain redact <id>` again after any restore.
- To abandon a redaction that was stopped before it committed, check `brain check`, then delete `<instance>/REDACTING`.
  Never delete it after the command printed "committed".
- If `brain check` reports `redaction_gate is not empty` or `redaction_packets is not empty`, restore the latest
  `post-redaction` bundle; no command clears them, by design.
- `brain check` and `brain redact` need `journal_mode = DELETE`; a store in WAL mode is refused.
- `brain check`, `brain backup` and `brain redact` ignore the planner statistics that `ANALYZE` writes
  (`sqlite_stat1`). A bundle leaves them out, and `brain restore` still refuses a bundle that carries them. Any other
  object that is not the engine's makes `brain redact` refuse before it changes anything.
- If the command prints `could not be checked and may hold the text` for a file, open that file yourself.

## Every other place the text can be

| Location | Who removes it | What to do |
|---|---|---|
| `<instance>/backups/` and each `--backups` folder | `brain redact` | It deletes every bundle of this brain older than the `post-redaction` one. A file it does not recognise (a bundle you renamed) is listed, not deleted: look at it |
| `<instance>/fetch/inbox/` | `brain redact` | It deletes the export files whose items carry the item's ref |
| A folder you gave to `brain backup --out` and did not name with `--backups` | You | Delete every bundle made before the redaction. `brain check` cannot see it |
| Hub `/backups` | `brain redact`, run in the container | As the first row |
| Hub offsite bucket | The company's admin | Use the bucket's lifecycle rule, or your provider's tools, from a machine that holds the delete-capable credential, never on the hub server (it holds a put-and-list credential only). (`./hub prune-offsite --remote NAME:PATH` does this from the admin machine.) |
| Bucket versioning or object lock | The company | Old versions go when the lifecycle rule expires them. Object lock blocks redaction until its retention ends, so choose lock or prompt redaction in advance. Redaction is complete only after that |
| VM disk snapshots | The company | Delete the provider's snapshots taken before the redaction |
| Time Machine and other machine backups | You | See below. Or keep `~/Brains` excluded, so it was never copied |
| A synced folder (iCloud Drive, Dropbox, a git repository) holding the instance or a bundle | You | Delete the old versions in the service's history. The brain does not keep an instance in one by default |
| Agent harness session logs and chat history | You | Delete or redact the session transcripts and tool output that show the text (every harness stores them, and `brain read` results carry quotes) |
| Another brain, or agent memory, that copied the text | You | Redact it there. Nothing links them |

### Time Machine

**Unverified.** The steps follow `man tmutil` (a page dated 10 June 2015) and were not run, because
they delete backups. Try them on a spare backup disk before you rely on them.

1. Stop future copies: `tmutil addexclusion ~/Brains/work`, then `tmutil isexcluded ~/Brains/work`. This needs no root.
2. List the backups made before the redaction: `sudo tmutil listbackups` (the terminal needs Full Disk Access).
3. Delete each: `sudo tmutil delete -d <backup disk mount point> -t <timestamp>`. This removes the **whole** backup,
   not just the brain. On an HFS+ disk, `-p <path>` can delete one path from a backup; the manual says this works for
   HFS backup disks only.
4. Local snapshots: `tmutil listlocalsnapshotdates`, then `sudo tmutil deletelocalsnapshots <YYYY-MM-DD-HHMMSS>`.

## How to check yourself

    grep -rlai -- "a phrase from the removed text" ~/Brains/work
    for b in ~/Brains/work/backups/*.tar.gz; do tar -xzOf "$b" brain.sqlite3 | grep -aci -- "a phrase from the removed text"; done

Both should find nothing. `-a` makes grep read the database (it holds NUL bytes, and `-I` would skip it); `-i` covers
both cases in one search. Use a phrase of a few words from the text.
