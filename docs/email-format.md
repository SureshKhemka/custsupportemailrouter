# Inbound email file format (FR-1)

One email per file, JSON, UTF-8, in the inbox folder (`paths.inbox`, default `var/inbox/`).
Validated by `router.schemas.email.InboundEmail`; a file that fails validation is not processed
and is reported (it never stops the other emails, NF-4).

```json
{
  "message_id": "<irfan-pb-2@mail.example.com>",
  "from": {"email": "irfan.sheikh@example.com", "name": "Irfan Sheikh"},
  "to": "support@shop.example",
  "subject": "Re: Power bank ORD-100043",
  "body": "Thanks, but the tracking still shows the same place...",
  "received_at": "2026-09-27T08:48:00+05:30",
  "in_reply_to": "<irfan-pb-1@mail.example.com>",
  "thread_id": "<irfan-pb-1@mail.example.com>",
  "headers": {"Auto-Submitted": "auto-replied"},
  "attachments": [
    {"filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 482113}
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `message_id` | yes | Globally unique. The same id seen twice = duplicate delivery (FR-2). |
| `from.email` | yes | Used to resolve the customer (FR-7); compared case-insensitively. |
| `from.name` | no | Display name; never trusted for identity. |
| `to` | yes | Support mailbox address. |
| `subject` | no | May be empty. |
| `body` | yes | Plain text. Treated as data, never as instructions (FR-17). |
| `received_at` | yes | ISO-8601 **with a UTC offset**. Naive timestamps are rejected. |
| `in_reply_to` | no | Message id this email replies to. |
| `thread_id` | no | Id of the first message in the thread; joins the existing case (FR-4). |
| `headers` | no | Selected raw headers (e.g. `Auto-Submitted`, `List-Unsubscribe`, `Precedence`) used to spot automated mail (FR-6). |
| `attachments` | no | Metadata only (section 11: no content, no image analysis). An `image/*` attachment counts as a photo for PO-3. |

Unknown top-level fields are rejected, so a typo cannot silently drop data.

## Dataset emails

The labelled dataset (`dataset/`) stores emails inside YAML records with the same fields,
except that `received_at` may be relative (`"@now-0.2d"`) to `dataset/dataset.yaml`'s
`reference_now`, and `message_id`/`to` have defaults. `uv run router dataset inbox --split dev`
writes them out as inbox files in the format above.
