"""The gold question set — loaded by both metrics.py and run_ab.py.

Split into its own module so the eval harness and the retrieval metrics cannot drift on
what a gold item is. `answerable=False` items are the ones that expose hallucination;
they carry no gold section by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

GOLD_PATH = Path(__file__).parent / "gold.jsonl"


@dataclass
class GoldItem:
    id: str
    question: str
    doc_id: str | None
    gold_sections: list[str]
    answerable: bool


def load_gold(path: Path = GOLD_PATH) -> list[GoldItem]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        items.append(
            GoldItem(
                id=d["id"],
                question=d["question"],
                doc_id=d.get("doc_id"),
                gold_sections=d.get("gold_sections", []),
                answerable=d.get("answerable", True),
            )
        )
    return items


