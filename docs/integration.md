# NexGen integration

The Windows app runs in local mode on first launch. It can import a local reservation schedule or use a separately provisioned HTTPS adapter. The current platform has an authenticated Review Studio importer for the `nexgen.session_record.v1` format. Live Studio exports it from an approved lesson. The website continues to own account identity, consent policy, mission progression and publishing.

## Schedule input

**Home → Import schedule** accepts the format in [example-reservations.json](example-reservations.json). Adjust its explicit time zone and date for local testing. A local import never creates network publication work. The ownership and enrolment checks for a real reservation belong to the server adapter.

A stable reservation ID identifies an appointment. Include an updated timestamp, start and end with offsets, learner ID, name, CEFR level and status. A cancellation only needs the reservation ID, `updated_at` and `status`. When multiple updates for one ID occur, the latest wins. Equal-time conflicting updates fail validation. A changed booking identity, mission or Meet link blocks capture against that draft. The currently shown schedule reflects the most recent successful import or fetch.

## Versioned HTTPS service

Configure a trusted HTTPS base URL in **Settings** and supply a scoped token. The URL is remembered. The token lives in memory for the current launch. Administrators can supply `NEXGEN_STUDIO_TOKEN` at startup on a controlled workstation. Adapter requests require TLS verification and do not follow redirects.

| Endpoint | Request and expected response |
| --- | --- |
| `GET {base}/reservations` | `Authorization: Bearer …` with a `nexgen.live_studio.reservations.v1` response scoped to the authenticated teacher |
| `POST {base}/lesson-results` | `Authorization: Bearer …`, `Content-Type: application/json`, `Idempotency-Key: {session_id}` with the approved `nexgen.live_studio.lesson.v1` payload |
| Acknowledgement | A successful response has `{"session_id":"…","status":"accepted"}` or the status `duplicate` for the same stored session |

The adapter must validate teacher, student, reservation, mission and consent before accepting, then commit the payload and idempotency key together. Automatic transmission is restricted to reservations obtained from that same configured adapter. A network or acknowledgement failure leaves the result queued with capped exponential backoff. Approved local reports are available for manual export. Both formats exclude private teaching-board notes. The legacy file can be imported by the current teacher review UI, which uses its own calculation and publication workflow.

## Evidence and retention

Stable session, sample and turn IDs are stored in SQLite with transactional approvals, revisions, and a durable outbox. Learner passports are reconstructed from approved lesson evidence. The same performance rows are not copied to a second history database.

The local audio bridge binds to `127.0.0.1:18763` and authenticates with an ephemeral key. A per-user Chrome native messaging host supplies that key to the bundled connector's exact extension ID. The connector reports only Meet room codes prior to browser audio authorization. The app retains no recording files. Its SQLite data is stored in `%LOCALAPPDATA%\NexGen\Live Studio` under the Windows user account. Disk encryption, backups and account access follow Windows workstation policy. Use Settings to choose local retention, and Home to delete a lesson and its local evidence. Pending adapter results are held until delivered or explicitly deleted. Deleting local evidence does not retract a previous export or an accepted NexGen result.
