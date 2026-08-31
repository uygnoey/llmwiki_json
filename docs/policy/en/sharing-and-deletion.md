# Sharing, Reuse and Deletion Consent

- Document version: v1.0-draft
- Effective date: {{EFFECTIVE_DATE}}

> This is a draft. Have it reviewed by counsel before launching the service.
> Separate consent to this document is collected at sign-up.

## 1. What this document covers

This service is a tool for **integrating** knowledge, not merely **referencing** it.
Reading someone else's document and folding its substance into your own is the normal
way to use it. As a result, "sharing" and "deletion" behave differently here than in
an ordinary file-sharing service. Please understand this difference before consenting.

## 2. What sharing means

1. A document posted to a **public group** can be read by other users of the Service.
2. Other users may link to it and cite it as a source in their own wiki.
3. Other users may read it and **write new documents of their own** based on it.
4. Documents in a **private group** are readable only by members, cannot be the target
   of outside references, and their existence and titles do not appear in any external
   graph.

## 3. What cannot be undone — please read

**Sentences another user wrote by integrating your document into their own wiki do
not disappear when you delete your document or close your account.**

Those sentences are that person's own work, and the Service has no means of
retrieving them. We do not promise deletion that assumes such retrieval. If you do not
want something disclosed irreversibly, **keep it in a private group from the start, or
turn off derivation for that document.**

## 4. Per-document settings

Each document carries its own settings:

| Setting | Meaning |
|---|---|
| `links` | Whether other documents may link to this one |
| `citation` | Whether other documents may cite this one as a source |
| `derivation` | Whether other users may integrate this content into their documents |
| `backlink` | Whether backlinks pointing at this document are shown to other users |

A per-document setting may only be **more restrictive than its group's setting, never
more open.** A document in a private group cannot be made public by its own setting.

## 5. Handling of original sources (raw)

1. **Original files such as PDFs and spreadsheets are not uploaded to the Service.**
   The client blocks them at the upload path.
2. When sharing, the original filename (`raw_ref`) and the full-text snapshot
   (`source_snapshot.text`) may be removed from the payload or replaced with a hash.
3. However, **excerpts and summaries you transcribed into wiki text are the document
   body itself and are therefore shared.** Not uploading the original file is a
   different matter from sentences drawn from it not being shared.

## 6. Choices at account closure

When closing your account you choose one of:

**A. Full deletion**
- Documents and blocks for which you are the author are deleted.
- In their place remains only a **tombstone** containing no personal data. This exists
  so that a broken link in another user's document reads as "deleted document" rather
  than "missing document".
- In co-edited documents, only the blocks you authored are deleted; the document
  itself remains.

**B. Transfer of ownership and retention**
- Ownership passes to your group and the content is kept.
- Author attribution may be replaced with an anonymised identifier.

## 7. What remains even after deletion

Even if you choose full deletion, the following remain:

1. **Derivative documents** other users wrote after reading yours.
2. Tombstones — containing only the document identifier and deletion time.
3. The minimum processing records we are legally required to retain.
4. Other users' local caches and already-distributed copies — removed progressively as
   they synchronise, with no guarantee of immediacy.

## 8. Consent checklist

At sign-up you must consent to each of the following:

- [ ] I understand that other users may read and cite documents I post to a public
      group.
- [ ] I understand that **derivative sentences written by other users do not disappear
      when I close my account.**
- [ ] I understand that original files are not uploaded, but content I transcribe into
      wiki text is shared.
- [ ] I will not upload the full text of third-party works or any security
      information.
