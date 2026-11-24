"""Local reservation input, approved NexGen exports and a versioned HTTPS outbox."""
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .analysis import LEVELS, eligible, report, tokens

MEET = re.compile(r"^https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$")
SCHEMA = "nexgen.live_studio.lesson.v1"


def meet_code(url):
    match = MEET.fullmatch(url or "")
    return match[1] if match else None


def when(value):
    result = datetime.fromisoformat(value)
    if not result.tzinfo:
        raise ValueError("Reservation times must include a time zone.")
    return result


def reservation_rows(document):
    if document.get("schema") != "nexgen.live_studio.reservations.v1":
        raise ValueError("Use a NexGen Live Studio reservation file.")
    found = {}
    for row in document["reservations"]:
        key = row["reservation_id"]
        updated = when(row["updated_at"])
        if not isinstance(key, str) or not key.strip() or row["status"] not in ("confirmed", "cancelled"):
            raise ValueError("Invalid reservation identity or status.")
        previous = found.get(key)
        if previous and updated == when(previous["updated_at"]) and row != previous:
            raise ValueError("Conflicting reservation updates.")
        if not previous or updated > when(previous["updated_at"]):
            found[key] = row
    for row in found.values():
        if row["status"] == "cancelled":
            continue
        if (not row["student_id"].strip() or not row["name"].strip() or row["level"] not in LEVELS
                or when(row["end_at"]) <= when(row["start_at"])
                or row.get("meet_url") and not meet_code(row["meet_url"])):
            raise ValueError("A confirmed reservation has invalid learner, time or Meet information.")
    return list(found.values())


def upcoming(rows, clock=None):
    clock = clock or datetime.now(timezone.utc)
    offers = [row for row in rows if row["status"] == "confirmed"
              and clock - timedelta(minutes=10) <= when(row["end_at"])
              and when(row["start_at"]) <= clock + timedelta(days=7)]
    return sorted(offers, key=lambda row: when(row["start_at"]))


def suggested(offers, clock=None):
    clock = clock or datetime.now(timezone.utc)
    active = [r for r in offers if when(r["start_at"]) <= clock < when(r["end_at"])]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        return None
    future = [r for r in offers if when(r["start_at"]) > clock]
    if future and (len(future) == 1 or when(future[0]["end_at"]) <= when(future[1]["start_at"])):
        return future[0]
    return None


def bundle(lesson, metrics=None):
    """Publication snapshot. Private board notes and raw recognizer output stay local."""
    metrics = metrics or report(lesson["samples"], lesson["profile"])
    samples = []
    for sample in lesson["samples"]:
        evidence = []
        for turn in [] if sample["excluded"] else sample["turns"]:
            if turn["excluded"] or turn["flags"]:
                continue
            evidence.append({key: turn[key] for key in ("id", "speaker", "mode", "start_ms",
                             "end_ms", "text", "words", "teaching", "evidence", "error_count", "confirmed_grammar")})
        samples.append({**{key: sample[key] for key in ("id", "activity", "label", "excluded", "state")},
                        "metrics": report([sample], lesson["profile"]), "evidence": evidence})
    return {
        "schema": SCHEMA, "session_id": lesson["id"], "student_id": lesson["student_id"],
        "reservation_id": lesson["reservation_id"], "mission_id": lesson["mission_id"],
        "level": lesson["level"], "source": lesson["source"], "created_at": lesson["created_at"],
        "consent_confirmed_at": lesson["consent_at"], "approval": lesson["approval"],
        "metrics": metrics, "samples": samples, "profile": lesson["profile"],
        "teaching": [{key: item[key] for key in ("id", "term", "meaning", "definition", "example", "revision")}
                     for item in lesson["board"] if item["shared"]],
    }


def legacy_export(lesson):
    """Import-file compatibility with the existing NexGen Review Studio."""
    if not lesson["approval"]:
        raise ValueError("Approve this lesson before creating a Review Studio export.")
    segments = []
    # A resumed draft has a fresh monotonic clock. Lay out separate samples consecutively.
    offset = 0
    for sample in lesson["samples"]:
        if sample["excluded"]:
            continue
        for turn in sample["turns"]:
            if not eligible(turn) and (turn["speaker"] != "teacher" or turn["flags"]
                                       or turn["excluded"] or turn["mode"] != "assessment"):
                continue
            words = [{**word, "start_ms": word["start_ms"] + offset, "end_ms": word["end_ms"] + offset}
                     for word in turn["words"] if tokens(word["text"])]
            segments.append({"speaker": turn["speaker"], "text": turn["text"], "words": words,
                             "start_ms": turn["start_ms"] + offset, "end_ms": turn["end_ms"] + offset})
        offset = max((s["end_ms"] for s in segments), default=offset) + 4000
    return {"schema": "nexgen.session_record.v1",
            "session": {"id": lesson["id"], "student_id": lesson["student_id"],
                        "booking_id": lesson["reservation_id"], "consent_confirmed": True},
            "sample": lesson["source"] == "demo", "preferred_transcript": {"segments": segments},
            "channel_check": {"separation": "under_review"},
            "live_studio": bundle(lesson)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def https_endpoint(base, resource):
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTPS integration base URL without credentials in the URL.")
    return base.rstrip("/") + "/" + resource


def get_reservations(base, token):
    request = Request(https_endpoint(base, "reservations"), headers={"Authorization": "Bearer " + token,
                      "Accept": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=8) as response:
        return reservation_rows(json.loads(response.read(2_000_001)))


def sync_due(store, base, token, force=False):
    """The receiving service must honor the stable Idempotency-Key and acknowledge the exact session."""
    url = https_endpoint(base, "lesson-results")
    delivered = 0
    for row in store.outbox(due=not force):
        if row["delivered_at"]:
            continue
        payload = json.loads(row["payload"])
        if payload["source"] != "nexgen" or not payload["reservation_id"] or row["adapter_url"] != base:
            continue
        session_id = row["session_id"]
        request = Request(url, data=row["payload"].encode(), method="POST",
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                   "Idempotency-Key": session_id, "Accept": "application/json"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=8) as response:
                ack = json.loads(response.read(65536))
                if ack.get("session_id") != session_id or ack.get("status") not in ("accepted", "duplicate"):
                    raise ValueError("The adapter returned an invalid acknowledgement.")
        except HTTPError as error:
            store.sync_result(session_id, f"Server returned HTTP {error.code}.")
        except (URLError, TimeoutError):
            store.sync_result(session_id, "Network unavailable or connection timed out.")
        except (ValueError, AttributeError):
            store.sync_result(session_id, "The adapter returned an invalid acknowledgement.")
        else:
            store.sync_result(session_id)
            delivered += 1
    return delivered
