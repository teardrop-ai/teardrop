# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from labeling.contracts import Definition, Observation, ObservationRequest, ScoreResult, TargetDraft

Parser = Callable[[dict[str, Any], Definition, Any], Sequence[TargetDraft]]
Scorer = Callable[[dict[str, Any], Observation | None, Definition], ScoreResult]


class Provider(Protocol):
    def plan(self, target: TargetDraft, definition: Definition) -> ObservationRequest: ...

    async def fetch_batch(
        self,
        requests: Sequence[ObservationRequest],
        definition: Definition,
    ) -> Mapping[str, Observation]: ...


PARSERS: dict[tuple[str, str], Parser] = {}
PROVIDERS: dict[tuple[str, str], Provider] = {}
SCORERS: dict[tuple[str, str], Scorer] = {}


def register_parser(key: str, version: str, parser: Parser) -> None:
    PARSERS[(key, version)] = parser


def register_provider(key: str, version: str, provider: Provider) -> None:
    PROVIDERS[(key, version)] = provider


def register_scorer(key: str, version: str, scorer: Scorer) -> None:
    SCORERS[(key, version)] = scorer


def resolve_parser(key: str, version: str) -> Parser:
    try:
        return PARSERS[(key, version)]
    except KeyError as exc:
        raise LookupError("Registered prediction parser is unavailable") from exc


def resolve_provider(key: str, version: str) -> Provider:
    try:
        return PROVIDERS[(key, version)]
    except KeyError as exc:
        raise LookupError("Registered observation provider is unavailable") from exc


def resolve_scorer(key: str, version: str) -> Scorer:
    try:
        return SCORERS[(key, version)]
    except KeyError as exc:
        raise LookupError("Registered prediction scorer is unavailable") from exc
