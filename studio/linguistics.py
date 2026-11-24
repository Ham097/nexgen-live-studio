"""Pinned English dependency parser and high-confidence candidate rules."""
from collections import Counter
from functools import lru_cache
from threading import RLock

from .analysis import FUNCTION, LEMMAS, PHRASES, language, tokens

_GATE = RLock()
_DEPENDENCIES = {
    "perfect.present": ("VBN", "aux", {"have", "has"}),
    "aspect.present_progressive": ("VBG", "aux", {"am", "is", "are", "'m", "'s", "'re"}),
    "future.will": ("VB", "aux", {"will", "'ll"}),
    "modality.could": ("VB", "aux", {"could"}),
    "voice.be_passive": ("VBN", "auxpass", {"am", "is", "are", "was", "were", "been", "be"}),
}


@lru_cache(maxsize=1)
def parser():
    import en_core_web_sm
    return en_core_web_sm.load(exclude=["ner"])


def warm():
    with _GATE:
        parser()("We have finished the lesson.")


@lru_cache(maxsize=512)
def _extract(text):
    guessed_vocab, grammar, review, functions = language(text)
    with _GATE:
        parsed = parser()(text)
    vocabulary = Counter()
    for token in parsed:
        raw = tokens(token.text)
        if len(raw) != 1 or raw[0] in FUNCTION or token.pos_ in {"AUX", "DET", "PRON", "PROPN"}:
            continue
        lemma = tokens(token.lemma_)
        root = LEMMAS.get(raw[0], lemma[0] if len(lemma) == 1 else raw[0])
        if root not in FUNCTION:
            vocabulary[root] += 1
    vocabulary.update({phrase: guessed_vocab[phrase] for phrase in PHRASES if phrase in guessed_vocab})
    parsed_grammar = Counter()
    for token in parsed:
        for key, (tag, dependency, words) in _DEPENDENCIES.items():
            if token.tag_ == tag and any(child.dep_ == dependency and child.lower_ in words for child in token.children):
                parsed_grammar[key] += 1
    grammar.update({key: max(count, grammar.get(key, 0)) for key, count in parsed_grammar.items()})
    return {"vocabulary": dict(vocabulary), "grammar": grammar, "review": review, "functions": functions}


def extract(text):
    if not text.strip():
        return {"vocabulary": {}, "grammar": {}, "review": [], "functions": []}
    return _extract(text)
