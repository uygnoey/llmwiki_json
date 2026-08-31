# Privacy Policy

- Document version: v1.0-draft
- Effective date: {{EFFECTIVE_DATE}}
- Data controller: {{OPERATOR}}
- Contact: {{CONTACT}}

> This is a draft. Have it reviewed by counsel before launching the service.

## 1. What we collect

| Category | Items | Basis |
|---|---|---|
| Account | Identifier, email, display name | Performance of contract |
| Groups | Group membership, role, join time | Performance of contract |
| Documents | `authors`, `history`, `created`, `updated` on pages and blocks | Performance of contract |
| Access | Timestamp, request path, IP, user agent | Legitimate interest (security) |
| Invites | Hash of the invite token, expiry, use count | Performance of contract |

Document bodies are written by you and contain no personal data unless you put it
there. Do not put third parties' personal data into document text.

## 2. Why we use it

1. To provide the Service: storage, indexing, search, and graph display.
2. To evaluate group permissions and enforce access control.
3. To show contribution history and to determine what to delete on account closure.
4. To respond to incidents, prevent abuse, and investigate security events.

## 3. Where it is stored and for how long

1. Canonical documents and derived artifacts are stored in object storage
   ({{STORAGE_PROVIDER}}, region {{REGION}}).
2. Account, group, and permission records are stored in a separate database.
3. Retention
   - Account data: until you close your account
   - Documents: according to the option you choose on closure
   - Access logs: {{LOG_RETENTION_DAYS}} days
   - Tombstones: indefinitely (they contain no personal data)

## 4. Disclosure and processors

1. We do not disclose data to third parties except where required by law.
2. We use the following processors for infrastructure: {{SUBPROCESSORS}}.
3. Other users seeing your documents is not "disclosure to a third party" — it is the
   **result of the sharing settings you chose**.

## 5. Account closure

On closure we delete your account data without undue delay. For documents you choose
either **full deletion** or **transfer of ownership to the group and retention**.
Even full deletion has limits that cannot be undone technically; these are set out in
[Sharing, Reuse and Deletion Consent](sharing-and-deletion.md).

## 6. Your rights

You may request access, rectification, erasure, restriction of processing, and data
portability. Portability is provided as a download of the complete canonical JSON.
Send requests to {{CONTACT}}; we respond within {{RESPONSE_DAYS}} days.

## 7. Security measures

1. Encryption in transit (TLS) and at rest.
2. Presigned URLs with short expiry times.
3. Invite tokens stored as hashes only; the plaintext token is never retained.
4. Isolation rules for private-group documents enforced at build time.
5. Permission decisions made in a dedicated permissions database, not in storage ACLs.

## 8. Contact

{{CONTACT}}
