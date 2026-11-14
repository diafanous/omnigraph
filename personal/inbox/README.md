# Omnigraph Inbox: add your files to the ontology

Two ways in. Both make one `Document` node per file in your graph, keep the
original file, and log the result. Agents then build the ontology from the
Documents (see `../ONTOLOGY-AGENTS.md`).

## 1. Upload in the app

1. Double-click `start-inbox.cmd` (in this folder).
   The app opens at http://127.0.0.1:8765 in your browser.
2. Drop files on the box, or click it and choose files.
3. Optional: type a note for the agents, for example "precedents for the facade study".
4. Click **Upload**. Each file shows `added`, `duplicate` or `error`.

From a terminal you can do the same:

```
python personal\inbox\inbox_app.py --data-dir "<knowledge>" add file1.pdf file2.md --note "facade study"
```

## 2. Send by email

Set up once (Gmail example):

1. Turn on 2-Step Verification for the Google account, then create an
   **app password** (Google Account > Security > App passwords). Do not use
   your normal password.
2. In Gmail, create a label named `Omnigraph`.
3. Create a Gmail filter: **To** = `youraddress+omnigraph@gmail.com`,
   action **Apply the label: Omnigraph** (and optionally Skip the Inbox).
4. In the app, open the **Email connection** panel and fill in:
   - IMAP server `imap.gmail.com`, port `993`
   - Mailbox address: your Gmail address
   - App password: the 16-character app password
   - Folder: `Omnigraph`
   - Allowed senders: each address you will send from, one per line
5. Click **Test connection**, then **Save**. Tick **Check automatically**
   to check every few minutes while the app runs. **Check now** checks once.

To add files: email them as attachments to `youraddress+omnigraph@gmail.com`.
The email text also becomes a Document unless you untick that option.

### Email safety

- Mail from an address that is not on the allowed list is refused and logged.
- With **Require DKIM/DMARC pass** on, a message that claims an allowed
  address but fails sender authentication is refused. Mail you send to your
  own mailbox has no authentication header and is accepted only when the
  sender is the mailbox address itself.
- The app password is encrypted with Windows DPAPI in
  `knowledge\inbox\email-password.bin`. Only your Windows account can read it.
  **Remove password** in the app deletes it.
- Agents treat email text as data. They do not follow instructions in it.

### Check email without the app open

Windows Task Scheduler can run one check on a timer:

```
python "<clone>\personal\inbox\inbox_app.py" --data-dir "<knowledge>" check-email
```

## What gets stored

| Item | Where |
|---|---|
| `Document` node: title, file name, type, size, SHA-256, channel, sender, subject, time, note, extracted text | graph (`knowledge\graph.omni`) |
| Original bytes, files up to 20 MB | graph, as a Blob (`omnigraph blob get node Document <slug> content ...`) |
| Original file, every size | `knowledge\inbox\files\<slug>\` |
| One line per item received, refused or failed | `knowledge\inbox\ledger.jsonl` |

Text is extracted from txt, md, csv, json, html, docx, pptx and xlsx. PDF text
needs `pip install pypdf`. Other files are stored without text; agents can open
the original.

The same file twice is a `duplicate` and is not added again.

## Limits

- The app listens on 127.0.0.1 only. Other computers cannot reach it.
- Requests over 200 MB are refused.
- Upload and email write `Document` nodes to `main` directly. Agents then work
  on their own branches (see `../ONTOLOGY-AGENTS.md`).
