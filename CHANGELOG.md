# Changelog

## [Pilot Increment] - 2026-10-21

### Added
- **Immutable commission ledger** (`commission_ledger` table)
  - Every payment release writes an immutable row
  - Amounts stored as integer paisa (minor units) to avoid floating-point issues
  - Includes gross amount, commission, net payout, rate snapshot, and note
- **Technician earnings statement**
  - `GET /api/tech/statement` (phone + PIN authenticated)
  - Returns ledger history and current wallet balance
- **Address privacy**
  - Full customer address is hidden in the public tracking view until the job status is `accepted`, `work_done`, or `confirmed`
  - Only area is shown before technician acceptance
- **Structured dispute flow**
  - `POST /api/track/{token}/dispute` — customer can raise a dispute with reason and evidence
  - `POST /api/admin/disputes/{request_id}/resolve` — admin resolution with optional refund adjustment written to the ledger
- **Quote revision history**
  - `quote_revisions` table
  - `POST /api/admin/requests/{rid}/quote-revision` — record a new quote with reason
  - `GET /api/requests/{rid}/quotes` — list all revisions for a request
- **Jinnah Garden pilot keywords**
  - `JINNAH_GARDEN_KEYWORDS` constant added for future area-based filtering

### Changed
- Removed the old simple dispute endpoint (replaced by the structured version above)
- Payment release now writes to both the existing `payments` table and the new immutable `commission_ledger`

### Notes
- Offer expiry (unaccepted assignments older than `ASSIGN_TIMEOUT_MIN` are re-queued) was already present in the sweeper and remains unchanged
- This increment is intended for the Jinnah Garden pilot
- Real payment gateway integration, true escrow, and production database migration are still out of scope

### Files changed
- `main.py` (+~170 lines net)
