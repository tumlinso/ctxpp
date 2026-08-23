from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ctxpp_lib import Tokenizer, stable_json


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


@dataclass
class BoundedPacketTelemetry:
    """Private, bounded packet observations for evaluations and adapters."""

    max_events: int = 64
    _events: deque[dict[str, Any]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise ValueError("max_events must be positive")
        self._events = deque(maxlen=self.max_events)

    def observe(
        self,
        packet: dict[str, Any],
        *,
        latency_ms: float,
        tokenizer: Tokenizer,
        compact_text: str,
        canonical_repository_tokens: int,
        broad_source_fallback: bool = False,
        local_worker_success: bool | None = None,
        accepted_patch: bool | None = None,
        codex_reinvestigation: bool | None = None,
        extra_source_reads: int | None = None,
        worker_result: str | None = None,
    ) -> dict[str, Any]:
        packet_tokens = tokenizer.count(stable_json(packet)).count
        compact_tokens = tokenizer.count(compact_text).count
        if "target" in packet:
            target_tokens = tokenizer.count(str(packet["target"]["content"])).count
        else:
            target_tokens = sum(tokenizer.count(str(item["content"])).count for item in packet.get("canonical_targets", []))
        trust = packet.get("trust", {})
        freshness = {
            "canonical_target": (
                trust.get("target_range") == "hash-verified"
                or trust.get("freshness") == "hash-verified"
            ),
            "relationships": (
                trust.get("relationships") == "semantic"
                and not trust.get("index_incomplete", True)
            ) if packet.get("schema_version") == 1 else trust.get("relationships") == "semantic",
        }
        event = {
            "budget_tokens": int(packet["request"]["budget_tokens"]),
            "packet_latency_ms": round(max(latency_ms, 0.0), 3),
            "freshness": freshness,
            "exact_packet_tokens": packet_tokens,
            "compact_inspect_tokens": compact_tokens,
            "canonical_repository_tokens": canonical_repository_tokens,
            "canonical_target_tokens": target_tokens,
            "canonical_source_avoided_tokens": max(canonical_repository_tokens - target_tokens, 0),
            "expansion_handles": len(packet.get("expansions", [])),
            "broad_source_fallbacks": int(broad_source_fallback),
            "local_worker_success": local_worker_success,
            "accepted_patch": accepted_patch,
            "codex_reinvestigation": codex_reinvestigation,
        }
        if extra_source_reads is not None:
            event["extra_source_reads"] = max(0, int(extra_source_reads))
        if worker_result is not None:
            event["worker_result"] = str(worker_result)
        self._events.append(event)
        return dict(event)

    def snapshot(self) -> dict[str, Any]:
        events = list(self._events)
        availability = {
            key: any(event[key] is not None for event in events)
            for key in ("local_worker_success", "accepted_patch", "codex_reinvestigation")
        }
        return {
            "format": "CTXPP-PACKET-TELEMETRY/1",
            "bounded_to": self.max_events,
            "events": events,
            "summary": {
                "observations": len(events),
                "mean_packet_latency_ms": _mean([float(event["packet_latency_ms"]) for event in events]),
                "fresh_canonical_targets": sum(bool(event["freshness"]["canonical_target"]) for event in events),
                "fresh_relationship_sets": sum(bool(event["freshness"]["relationships"]) for event in events),
                "expansion_handles": sum(int(event["expansion_handles"]) for event in events),
                "broad_source_fallbacks": sum(int(event["broad_source_fallbacks"]) for event in events),
                "availability": availability,
            },
        }
