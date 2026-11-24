"""Versioned observations derived from the current, eligible transcript."""
from collections import Counter
import re
from statistics import median

VERSION = "nexgen.studio.metrics.v1"
LEVELS = ("A1", "A2", "B1", "B2", "C1")
ACTIVITIES = ("Unlabelled", "Finish & Expand", "Compare & Choose", "Scenario & Role-Play")
OUTCOMES = ("Passed", "Further practice required", "Insufficient evidence", "Review required")
TOKEN = re.compile(r"[a-z]+(?:['-][a-z]+)*", re.I)
FILLER = re.compile(r"(?:u+h+m*|u+m+|e+r+m*|e+h+|e{2,}|h+m+|m{2,}|a+h+|o+h+)")
FUNCTION = set("a an the i you he she it we they me him her us them my your his its our their this that these those and or but of to in on at for is am are was were be been being do does did have has had can could would should will shall may might must with by from as not".split())
FORMS = {
    "go": "goes going went gone", "make": "makes making made", "say": "says saying said",
    "take": "takes taking took taken", "learn": "learns learned learnt learning",
    "study": "studies studied studying", "teach": "teaches teaching taught",
    "work": "works working worked", "use": "uses using used", "give": "gives giving gave given",
    "choose": "chooses choosing chose chosen", "compare": "compares comparing compared",
    "travel": "travels traveling travelling traveled travelled", "book": "books booking booked",
    "child": "children", "person": "people", "lesson": "lessons", "example": "examples",
    "plan": "plans planned planning", "help": "helps helped helping", "feel": "feels felt feeling",
}
LEMMAS = {form: lemma for lemma, forms in FORMS.items() for form in forms.split()}
PHRASES = ("for example", "in my opinion", "on the other hand", "look forward to",
           "take part in", "as soon as", "make sure", "a lot of")
RULES = {
    "tense.present_simple": r"\b(?:i|you|we|they) (?:work|live|study|like|want|need)\b",
    "aspect.present_progressive": r"\b(?:am|is|are) (?:working|learning|studying|waiting|going|trying|looking|speaking)\b",
    "past.irregular": r"\b(?:i|you|he|she|we|they) (?:went|saw|took|made|bought|found|got|said)\b",
    "perfect.present": r"\b(?:have|has) (?:been|gone|seen|done|worked|learned|finished|tried|made)\b",
    "future.will": r"\bwill (?:go|be|have|try|make|take|work|see|help|visit|finish)\b",
    "modality.could": r"\bcould (?:go|be|have|try|make|take|work|see|help|visit|finish)\b",
    "questions.do_support": r"(?:^|[.!?]\s*)(?:do|does|did) (?:i|you|he|she|we|they)\b",
    "negation.do_support": r"\b(?:don't|doesn't|didn't|do not|does not|did not)\b",
    "comparison.more_than": r"\bmore [a-z]+ than\b",
    "determiners.existential": r"\bthere (?:is|are|was|were)\b",
    "conditionals.if_would": r"\bif\b[^.!?]{3,100}\b(?:would|could)\b",
    "subordination.because": r"\bbecause (?:i|you|he|she|it|we|they)\b",
    "voice.be_passive": r"\b(?:is|are|was|were|been) (?:made|built|written|taught|invited|sent|given) by\b",
    "complements.want_to": r"\b(?:want|wanted|wants) to [a-z]+\b",
    "discourse.contrast": r"\b(?:on the other hand|however|even though)\b",
}
REVIEW = {
    "agreement.third_person": r"\b(?:he|she) (?:don't|do not|have a|go to)\b",
    "agreement.be": r"\b(?:they is|we is|i are|she are)\b",
    "verb.did_past": r"\bdid(?:n't| not)? (?:you |he |she |we |they )?(?:went|saw|took|made|bought)\b",
}
FUNCTIONS = {
    "example": r"\b(?:for example|for instance)\b",
    "opinion": r"\b(?:i think|in my opinion|i believe)\b",
    "justification": r"\b(?:because|the reason is)\b",
    "comparison": r"\b(?:than|compared with|whereas)\b",
    "clarification": r"\b(?:what i mean|in other words)\b",
}


def tokens(text):
    return [v for v in TOKEN.findall(text.lower().replace("’", "'")) if not FILLER.fullmatch(v)]


def language(text):
    normalized = text.lower().replace("’", "'")
    vocabulary = Counter(LEMMAS.get(word, word) for word in tokens(normalized) if word not in FUNCTION)
    vocabulary.update({phrase: len(re.findall(rf"\b{phrase}\b", normalized)) for phrase in PHRASES
                       if re.search(rf"\b{phrase}\b", normalized)})
    grammar = {key: len(list(re.finditer(rule, normalized))) for key, rule in RULES.items()
               if re.search(rule, normalized)}
    review = [key for key, rule in REVIEW.items() if re.search(rule, normalized)]
    functions = [key for key, rule in FUNCTIONS.items() if re.search(rule, normalized)]
    return vocabulary, grammar, review, functions


def eligible(turn):
    return (turn["speaker"] == "student" and turn["mode"] == "assessment"
            and turn["evidence"] in ("observed", "independent") and not turn["flags"]
            and not turn["excluded"] and bool(tokens(turn["text"])))


def interval_duration(intervals):
    total = end = 0
    for start, finish in sorted(intervals):
        if finish > end:
            total += finish - max(start, end)
            end = finish
    return total


def report(samples, profile):
    """Bound timing to one uninterrupted mode/window and teacher-delimited response."""
    vocab, independent, grammar, grammar_independent = Counter(), Counter(), Counter(), Counter()
    confirmed, unconfirmed, functions, confirmed_grammar = Counter(), Counter(), Counter(), Counter()
    words = timed_words = spoken_ms = response_ms = accuracy_words = 0
    pauses, thinking, runs, activities = [], [], [], []
    pause_ms = profile["pause_ms"]
    for sample in samples:
        if sample["excluded"]:
            continue
        if any(eligible(turn) for turn in sample["turns"]):
            activities.append(sample["activity"])
        windows = {}
        for turn in sorted(sample["turns"], key=lambda row: (row["start_ms"], row["speaker"])):
            state = windows.setdefault(turn["epoch"], {"previous": None, "response": []})
            previous = state["previous"]
            if not eligible(turn):
                state["response"] = []
                state["previous"] = turn
                continue
            word_count = len(tokens(turn["text"]))
            words += word_count
            features = turn.get("features")
            if features:
                observed, structures, candidates, markers = (features[key] for key in
                                                              ("vocabulary", "grammar", "review", "functions"))
            else:
                observed, structures, candidates, markers = language(turn["text"])
            vocab.update(observed)
            grammar.update(structures)
            functions.update(markers)
            if turn["evidence"] == "independent":
                independent.update(observed)
                grammar_independent.update({key: structures[key]
                                            for key in turn.get("confirmed_grammar", []) if key in structures})
            confirmed_grammar.update(key for key in turn.get("confirmed_grammar", []) if key in structures)
            if turn["error_count"] is None:
                unconfirmed.update(candidates)
            else:
                accuracy_words += word_count
                confirmed["count"] += turn["error_count"]
            stamps = [(word["start_ms"], word["end_ms"]) for word in turn["words"]
                      if tokens(word["text"]) and word["end_ms"] > word["start_ms"]]
            # Edited words have no fresh alignment. Count their language but exclude their timing.
            if len(stamps) != word_count:
                state["previous"] = turn
                state["response"] = []
                continue
            if previous and previous["speaker"] == "teacher" and previous["end_ms"] <= stamps[0][0]:
                thinking.append(stamps[0][0] - previous["end_ms"])
            response = state["response"]
            gap = stamps[0][0] - response[-1][1] if response else 0
            if response and (gap < 0 or gap > profile["response_gap_ms"]):
                response = []
            if not response:
                response = []
                runs.append(response)
                state["response"] = response
            response.extend(stamps)
            state["previous"] = turn
    for run in runs:
        response_ms += max(end for _, end in run) - run[0][0]
        timed_words += len(run)
        spoken_ms += interval_duration(run)
        for previous, current in zip(run, run[1:]):
            gap = current[0] - previous[1]
            if gap >= pause_ms:
                pauses.append(gap)
    sufficient = timed_words >= profile["min_timed_words"] and response_ms >= profile["min_response_ms"]
    return {
        "version": VERSION, "words": words, "timed_words": timed_words,
        "response_ms": response_ms, "speaking_ms": spoken_ms,
        "wpm": round(timed_words * 60000 / response_ms, 1) if sufficient and response_ms else None,
        "articulation_wpm": round(timed_words * 60000 / spoken_ms, 1) if sufficient and spoken_ms else None,
        "flow": round(timed_words / (len(pauses) + len(runs)), 1) if sufficient and runs else None,
        "pause_count": len(pauses), "pause_median_ms": median(pauses) if pauses else None,
        "thinking_median_ms": median(thinking) if thinking else None,
        "vocabulary": dict(vocab), "independent_vocabulary": dict(independent),
        "vocabulary_diversity": round(len(vocab) / words, 3) if words else None,
        "grammar": dict(grammar), "confirmed_grammar": dict(confirmed_grammar),
        "independent_grammar": dict(grammar_independent),
        "possible_errors": dict(unconfirmed), "validated_errors": confirmed["count"],
        "validated_words": accuracy_words,
        "errors_per_100": round(100 * confirmed["count"] / accuracy_words, 1)
        if accuracy_words >= profile["min_timed_words"] else None,
        "functions": dict(functions), "activities": sorted(set(activities)),
        "required_activities": profile["required_activities"], "comparable": sufficient,
    }
