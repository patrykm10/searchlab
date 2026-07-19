"""Synthetic document generation from declarative YAML profiles.

The interesting Solr performance pathologies come from data *shape*, not raw
count: field cardinality, text length distribution, value skew, multivalued
fields. Profiles let you dial each of these in.
"""

from __future__ import annotations

import json
import math
import random
import string
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# Small embedded wordlist keeps the tool dependency-free and deterministic.
_WORDS = (
    "search index shard replica segment merge commit query facet filter cache "
    "cluster node leader follower zookeeper overseer collection schema field "
    "token analyzer stemmer boost score rank latency throughput heap garbage "
    "collector pause allocation buffer flush translog document term posting "
    "dictionary automaton transducer skip list block tree codec stored norm "
    "vector highway mountain river ocean forest desert village castle bridge "
    "engine rocket signal orbit crystal thunder ember willow falcon harbor "
    "quantum lattice cipher beacon summit canyon meadow glacier aurora zenith"
).split()


class FieldGen:
    """One field generator, built from its profile definition."""

    def __init__(self, name: str, cfg: dict[str, Any], rng: random.Random):
        self.name = name
        self.cfg = cfg
        self.rng = rng
        self.type = cfg.get("type", "text")
        # Vectors: pre-build cluster centroids. Uniform random vectors are
        # pathological for ANN benchmarking (everything is equidistant), so we
        # generate around centroids — the shape real embeddings actually have.
        if self.type == "vector":
            self.dims = int(cfg.get("dims", 768))
            n_clusters = int(cfg.get("clusters", 8))
            self.cluster_std = float(cfg.get("cluster_std", 0.15))
            self.centroids = [
                self._unit([rng.gauss(0, 1) for _ in range(self.dims)])
                for _ in range(n_clusters)
            ]
        # Pre-build the value pool for categorical fields.
        if self.type == "categorical":
            card = int(cfg.get("cardinality", 10))
            prefix = cfg.get("prefix", name)
            self.pool = [f"{prefix}_{i}" for i in range(card)]
            self.skew = float(cfg.get("zipf", 0))  # 0 = uniform
            if self.skew > 0:
                weights = [1 / (i + 1) ** self.skew for i in range(card)]
                total = sum(weights)
                self.weights = [w / total for w in weights]
            else:
                self.weights = None

    @staticmethod
    def _unit(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def value(self, seq: int) -> Any:
        t = self.type
        if t == "id":
            if self.cfg.get("uuid"):
                # Derive from the seeded RNG (uuid4 uses os.urandom and would
                # break --seed reproducibility).
                return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
            return f"doc-{seq}"
        if t == "text":
            lo = int(self.cfg.get("min_words", 5))
            hi = int(self.cfg.get("max_words", 50))
            n = self.rng.randint(lo, hi)
            return " ".join(self.rng.choices(_WORDS, k=n))
        if t == "keyword":
            length = int(self.cfg.get("length", 8))
            return "".join(self.rng.choices(string.ascii_lowercase, k=length))
        if t == "categorical":
            if self.weights:
                return self.rng.choices(self.pool, weights=self.weights, k=1)[0]
            return self.rng.choice(self.pool)
        if t == "int":
            return self.rng.randint(int(self.cfg.get("min", 0)), int(self.cfg.get("max", 1000)))
        if t == "float":
            return round(self.rng.uniform(float(self.cfg.get("min", 0)), float(self.cfg.get("max", 1000))), 4)
        if t == "date":
            days = int(self.cfg.get("days_back", 365))
            dt = datetime.now(timezone.utc) - timedelta(
                seconds=self.rng.randint(0, days * 86400)
            )
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if t == "bool":
            return self.rng.random() < float(self.cfg.get("true_ratio", 0.5))
        if t == "vector":
            c = self.rng.choice(self.centroids)
            v = [ci + self.rng.gauss(0, self.cluster_std) for ci in c]
            return [round(x, 4) for x in self._unit(v)]
        if t == "multivalued":
            inner = FieldGen(self.name, self.cfg.get("of", {"type": "keyword"}), self.rng)
            lo = int(self.cfg.get("min_values", 1))
            hi = int(self.cfg.get("max_values", 5))
            return [inner.value(seq) for _ in range(self.rng.randint(lo, hi))]
        sys.exit(f"solrlab: unknown field type '{t}' for field '{self.name}'")


def load_profile(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text())
    if "fields" not in data:
        sys.exit(f"solrlab: profile {path} has no 'fields' section")
    return data


def generate(profile: dict[str, Any], count: int, seed: int | None = None) -> Iterator[dict]:
    rng = random.Random(seed)
    gens = [FieldGen(name, cfg or {}, rng) for name, cfg in profile["fields"].items()]
    for seq in range(count):
        yield {g.name: g.value(seq) for g in gens}


def generate_to_file(
    profile_path: str | Path, count: int, out: str | Path, seed: int | None = None
) -> int:
    profile = load_profile(profile_path)
    n = 0
    with open(out, "w") as f:
        for doc in generate(profile, count, seed):
            f.write(json.dumps(doc) + "\n")
            n += 1
    return n
