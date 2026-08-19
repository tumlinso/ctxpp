from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable


GENERIC_TERMS = {"data", "item", "result", "state", "value", "workspace"}


def terms(text: str) -> list[str]:
    out: list[str] = []
    word: list[str] = []
    previous = ""
    for char in text:
        if char.isalnum():
            if word and char.isupper() and (previous.islower() or previous.isdigit()):
                out.append("".join(word).lower())
                word = []
            word.append(char)
            previous = char
        else:
            if word:
                out.append("".join(word).lower())
                word = []
            previous = ""
    if word:
        out.append("".join(word).lower())
    return [value for value in out if value]


def rank(query: str, candidates: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    query_terms = list(dict.fromkeys(terms(query)))
    values = list({str(candidate.get("id")): candidate for candidate in candidates}.values())
    if not query_terms or not values:
        return []
    documents = []
    frequency: Counter[str] = Counter()
    for candidate in values:
        fields = _fields(candidate)
        present = {term for term in query_terms if any(term in field for field in fields.values())}
        documents.append((candidate, fields, present))
        frequency.update(present)
    ranked = []
    for candidate, fields, present in documents:
        if not present:
            continue
        informative = present - GENERIC_TERMS
        if len(query_terms) > 1 and len(present) == 1 and not informative:
            continue
        coverage = len(present) / len(query_terms)
        score = 0.0
        name_tokens = terms(fields["name"])
        qname_tokens = terms(fields["qname"])
        if fields["qname"] == query.lower():
            score += 12
        if fields["name"] == query.lower():
            score += 10
        for term in present:
            idf = math.log((len(values) + 1) / (frequency[term] + 1)) + 1
            if term in name_tokens or term in qname_tokens:
                score += 8 * idf
            elif term in fields["path"] or term in fields["container"]:
                score += 4 * idf
            elif term in fields["signature"] or term in fields["contract"]:
                score += 3 * idf
            else:
                score += idf
        same_neighborhood = any(all(term in fields[field] for term in query_terms)
                                for field in ("qname", "container", "path", "signature", "contract", "lexical"))
        if same_neighborhood:
            score += 6
        elif coverage < 0.67 and len(query_terms) >= 3:
            score -= 6
        if present <= GENERIC_TERMS:
            score -= 6
        score += 2 * bool(candidate.get("definition")) + int(candidate.get("_route_test_bonus", 0))
        if coverage >= 1:
            confidence = "high"
        elif coverage >= 0.67 and informative:
            confidence = "medium"
        else:
            confidence = "low"
        ranked.append((score, coverage, {**candidate, "_route_confidence": confidence}))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].get("qualified_name", ""), item[2].get("file", "")))
    return [candidate for score, _, candidate in ranked if score > 0][:limit]


def _fields(candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(candidate.get("name", "")).lower(),
        "qname": str(candidate.get("qualified_name", "")).lower(),
        "container": str(candidate.get("containing_type", candidate.get("parent_name", ""))).lower(),
        "path": str(candidate.get("file", "")).lower(),
        "signature": str(candidate.get("signature", "")).lower(),
        "contract": str(candidate.get("contract", "")).lower(),
        "lexical": str(candidate.get("lexical_terms", "")).lower(),
    }
