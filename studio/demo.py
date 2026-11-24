"""Written fictional evidence for UI review. No microphone or learner data."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .analysis import tokens


def populate(store):
    learner = {"student_id": "fictional-maya", "name": "Maya Ellis", "level": "B1",
               "mission_id": "Compare travel options", "source": "demo"}
    turns = [
        ("teacher", "What would you choose for your trip?"),
        ("student", "In my opinion, we should take the train because we can work on the way."),
        ("student", "For example, I can take my laptop and finish the report before we arrive."),
        ("teacher", "What about flying?"),
        ("student", "On the other hand, the plane is more expensive than the train, but it takes less time."),
        ("student", "If we had more money, we could take the plane and book a hotel for one extra night."),
    ]

    def make_sample(lesson, sequence, interval, activity, name, epoch):
        sample = store.sample(lesson, activity)
        stamp = 1000
        for speaker, text in sequence:
            words = []
            for index, word in enumerate(tokens(text)):
                if index == 6:
                    stamp += 850
                words.append({"text": word, "start_ms": stamp, "end_ms": stamp + interval - 40,
                              "probability": .96})
                stamp += interval
            key = store.add_turn({"sample_id": sample, "epoch": epoch, "speaker": speaker,
                                  "mode": "assessment", "start_ms": words[0]["start_ms"],
                                  "end_ms": words[-1]["end_ms"], "original": text, "words": words, "flags": []})
            if speaker == "student":
                from .linguistics import extract
                store.revise(key, evidence="independent", error_count=0,
                             confirmed_grammar=list(extract(text)["grammar"]))
            stamp += 1230
        store.sample_action(sample, label=name, state="finished")

    for days, rate in ((15, 570), (8, 510), (2, 475)):
        old = store.create(learner, True)
        with store.db() as db:
            db.execute("UPDATE sessions SET created_at=? WHERE id=?",
                       ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), old))
        make_sample(old, turns[:3], rate, "Compare & Choose", "Train", str(uuid4()))
        make_sample(old, turns[3:], rate, "Compare & Choose", "Plane", str(uuid4()))
        store.approve(old, "Passed", "Demo teacher", "")
    current = store.create(learner, True)
    make_sample(current, turns[:3], 435, "Compare & Choose", "Train option", str(uuid4()))
    make_sample(current, turns[3:], 435, "Compare & Choose", "Plane option", str(uuid4()))
    store.board_item(current, term="on the other hand", definition="Use this to introduce a contrasting point.",
                     meaning="öte yandan", shared=1)
    store.board_item(current, term="before we arrive", example="I can finish the report before we arrive.", shared=1)
    store.board_item(current, term="more expensive than", revision=1)
    return current
