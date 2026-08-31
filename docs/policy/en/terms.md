# Terms of Service

- Document version: v1.0-draft
- Effective date: {{EFFECTIVE_DATE}}
- Operator: {{OPERATOR}}
- Contact: {{CONTACT}}

> This is a draft. Have it reviewed by counsel before launching the service.

## 1. About the service

This service (the "Service") stores knowledge documents you write as canonical JSON,
shares them at the group level, and lets you combine documents from several groups
into one view. Documents live in object storage; derived artifacts (indexes, graphs)
are regenerated from the canonical files.

## 2. Accounts and groups

1. You create an account and may belong to one or more groups.
2. A group is either **public** or **private**.
   - Documents in a public group are readable by users of the Service.
   - Documents in a private group are readable only by members and cannot be the
     target of references from outside documents.
3. A private group can be joined only through an **invite link**. Invite links carry
   an expiry time, a use limit, and the role they grant (read-only or editor).
4. Group owners may change a member's role or remove them.

## 3. Copyright and licence

1. You **retain copyright** in the documents you write.
2. You grant the Service a non-exclusive licence to store, reproduce, display, and
   transmit those documents to the extent needed to operate the Service.
3. By posting to a public group you agree that other users may read your document and
   integrate it into their own wiki. See
   [Sharing, Reuse and Deletion Consent](sharing-and-deletion.md) for the details.
4. You may set per-document permissions for linking, citation, and derivation.
   A per-document setting may only be **more** restrictive than its group's setting,
   never more open.

## 4. What must not be uploaded

1. **Original source files** — PDFs, spreadsheets, and anything else belonging in
   `raw/` are not upload targets; the client blocks them at the upload path.
2. The whole or a substantial part of third-party works you have no right to
   distribute.
3. Credentials, connection details, secret keys, or other security information.
   In wiki text, replace these with `(connection details omitted)`.
4. Personal data of third parties that you have no right to share.
5. Anything unlawful or infringing on the rights of others.

## 5. Prohibited conduct

1. Attempting to access documents in a group you have no access to.
2. Distributing invite links to unauthorised third parties.
3. Circumventing the Service's access control or isolation rules.
4. Automated bulk collection that interferes with operating the Service.

## 6. Suspension and termination

1. The Operator may suspend the Service for maintenance, incidents, or legal
   compliance, giving advance notice where possible.
2. You may close your account at any time. Data handling on account closure follows
   [Sharing, Reuse and Deletion Consent](sharing-and-deletion.md).
3. The Operator may restrict use in cases of serious breach of Sections 4 or 5.

## 7. Disclaimer

The Service makes no warranty as to the accuracy of the documents users write.
Documents are authored jointly by users and automated tooling, and may contain errors
or stale claims. Verify the original sources before relying on a document for any
consequential decision.

## 8. Changes to these terms

We give at least 30 days' notice before a change takes effect. If you do not accept a
change that disadvantages you, you may close your account.

## 9. Governing law and jurisdiction

These terms are governed by {{GOVERNING_LAW}}, and disputes are subject to the
jurisdiction of {{JURISDICTION}}.
