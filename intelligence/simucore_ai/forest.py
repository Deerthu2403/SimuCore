"""A trained Isolation Forest as plain arrays.

Training uses scikit-learn. What is stored and used in operation is this copy of its trees: it gives
the same scores (a test compares them), needs no pickle, which could run code when loaded, and no
scikit-learn at run time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CHUNK = 4096


def average_path(n: np.ndarray) -> np.ndarray:
    """Mean depth of an unsuccessful search in a binary tree of n points."""
    n = np.asarray(n, float)
    out = np.zeros_like(n)
    out[n == 2] = 1.0
    big = n > 2
    out[big] = 2 * (np.log(n[big] - 1) + np.euler_gamma) - 2 * (n[big] - 1) / n[big]
    return out


@dataclass(frozen=True)
class Forest:
    root: np.ndarray  # (trees,) index of the first node of each tree
    feature: np.ndarray  # (nodes,) -1 in a leaf
    threshold: np.ndarray
    left: np.ndarray
    right: np.ndarray
    depth: np.ndarray  # (nodes,) path length that ends here, including what an unsplit leaf hides
    samples: int

    @classmethod
    def from_sklearn(cls, model) -> Forest:
        root, feature, threshold, left, right, depth = [], [], [], [], [], []
        offset = 0
        for tree in (e.tree_ for e in model.estimators_):
            n = tree.node_count
            level = np.zeros(n)
            for i in range(n):  # parents come before their children
                if tree.children_left[i] >= 0:
                    level[tree.children_left[i]] = level[tree.children_right[i]] = level[i] + 1
            leaf = tree.children_left < 0
            root.append(offset)
            feature.append(np.where(leaf, -1, tree.feature))
            threshold.append(tree.threshold)
            left.append(np.where(leaf, 0, tree.children_left + offset))
            right.append(np.where(leaf, 0, tree.children_right + offset))
            depth.append(np.where(leaf, level + average_path(tree.n_node_samples), 0.0))
            offset += n
        return cls(np.array(root, np.int32), np.concatenate(feature).astype(np.int16),
                   np.concatenate(threshold).astype(np.float64),
                   np.concatenate(left).astype(np.int32), np.concatenate(right).astype(np.int32),
                   np.concatenate(depth).astype(np.float64), int(model.max_samples_))

    def score(self, x: np.ndarray) -> np.ndarray:
        """0..1, larger = more unusual. The same as ``-score_samples`` of scikit-learn."""
        x = np.asarray(x, np.float32)
        if len(x) > CHUNK:
            return np.concatenate([self.score(x[i: i + CHUNK]) for i in range(0, len(x), CHUNK)])
        node = np.broadcast_to(self.root, (len(x), len(self.root))).copy()
        rows = np.arange(len(x))[:, None]
        while True:
            f = self.feature[node]
            inner = f >= 0
            if not inner.any():
                break
            go_left = x[rows, np.maximum(f, 0)] <= self.threshold[node]
            node = np.where(inner, np.where(go_left, self.left[node], self.right[node]), node)
        mean = self.depth[node].mean(axis=1)
        return 2.0 ** (-mean / average_path(np.array([self.samples]))[0])

    def arrays(self) -> dict[str, np.ndarray]:
        return {k: getattr(self, k) for k in ("root", "feature", "threshold", "left", "right",
                                              "depth")} | {"samples": np.array(self.samples)}

    @classmethod
    def from_arrays(cls, a: dict[str, np.ndarray]) -> Forest:
        n = len(a["feature"])
        for k in ("threshold", "left", "right", "depth"):
            if len(a[k]) != n:
                raise ValueError("damaged forest")
        if (a["left"] >= n).any() or (a["right"] >= n).any() or (a["root"] >= n).any():
            raise ValueError("damaged forest")
        return cls(**{k: a[k] for k in ("root", "feature", "threshold", "left", "right", "depth")},
                   samples=int(a["samples"]))
