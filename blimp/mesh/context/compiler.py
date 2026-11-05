"""Blimp Crew v2 — Context Compiler (spec §11.6): builds each turn's context pack.

Under token budgets, ordered for prompt caching (static first). Zero LLM.
Everything is a reference first; untrusted content never enters raw; the manifest
(layer ids, versions, hashes, token counts) is recorded on turn.started (fixes F3/L-017).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Layer order = cache order (static -> volatile). Budgets for a 200K window [ASSUMPTION].
LAYER_BUDGETS = [
    ("L0", "constitution", 2500, "static-all"),
    ("L1", "identity", 1500, "static-agent"),
    ("L2", "protocol", 1500, "static"),
    ("TOOLS", "tool_definitions", 8000, "static-capabilities"),
    ("L3", "playbook_skills_index", 2000, "semi-static"),
    ("PAGES", "knowledge_pages", 6000, "semi-static-daily"),
    ("L4", "task_contract", 2000, "per-task"),
    ("L5", "ledger", 3000, "per-turn"),
    ("DEC", "relevant_decisions", 4000, "per-task"),
    ("TAIL", "thread_tail", 6000, "per-turn"),
    ("MEM", "retrieved_memory", 6000, "per-turn"),
    ("L7", "trigger_batch", 6000, "per-turn"),
]


def _est(text: str) -> int:
    return max(1, int(len(text) / 3.8))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ContextPack:
    sections: list[tuple[str, str]] = field(default_factory=list)  # (layer_id, content)
    manifest: list[dict[str, Any]] = field(default_factory=list)

    @property
    def prompt_text(self) -> str:
        return "\n\n".join(f"<{sid}>\n{body}\n</{sid}>" for sid, body in self.sections)


def compile_pack(
    layers: dict[str, str],
    *,
    knowledge_pages: dict[str, str] | None = None,
    tool_definitions: str = "",
    task_contract: str = "",
    ledger: str = "",
    relevant_decisions: str = "",
    thread_tail: str = "",
    retrieved_memory: str = "",
    trigger_batch: str = "",
) -> ContextPack:
    """Assemble the pack in cache order, trimming each layer to its budget."""
    pages = knowledge_pages or {}
    pack = ContextPack()

    def add(layer_id: str, name: str, budget: int, text: str):
        if not text:
            return
        tokens = _est(text)
        if tokens > budget:
            text = text[: budget * 4] + "\n[truncated to budget]"
            tokens = budget
        pack.sections.append((layer_id, text))
        pack.manifest.append({
            "layer": layer_id, "name": name, "tokens": tokens,
            "sha256": _sha256(text),
        })

    for layer_id, name, budget, _cache in LAYER_BUDGETS:
        if layer_id == "L0":
            add(layer_id, name, budget, layers.get("constitution", ""))
        elif layer_id == "L1":
            add(layer_id, name, budget, layers.get("identity", ""))
        elif layer_id == "L2":
            add(layer_id, name, budget, layers.get("protocol", ""))
        elif layer_id == "TOOLS":
            add(layer_id, name, budget, tool_definitions)
        elif layer_id == "L3":
            add(layer_id, name, budget, layers.get("playbook", ""))
        elif layer_id == "PAGES":
            add(layer_id, name, budget, "\n\n".join(pages.values()))
        elif layer_id == "L4":
            add(layer_id, name, budget, task_contract)
        elif layer_id == "L5":
            add(layer_id, name, budget, ledger)
        elif layer_id == "DEC":
            add(layer_id, name, budget, relevant_decisions)
        elif layer_id == "TAIL":
            add(layer_id, name, budget, thread_tail)
        elif layer_id == "MEM":
            add(layer_id, name, budget, retrieved_memory)
        elif layer_id == "L7":
            add(layer_id, name, budget, trigger_batch)

    return pack


def pack_hash(pack: ContextPack) -> str:
    return _sha256(pack.prompt_text)


def manifest_json(pack: ContextPack) -> str:
    return json.dumps(pack.manifest, separators=(",", ":"))