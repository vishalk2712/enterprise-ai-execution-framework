"""Similarity primitives, without supplier-specific identity policy."""
def levenshtein_similarity(left, right):
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left)+1))
    for j, b in enumerate(right, 1):
        current = [j]
        for i, a in enumerate(left, 1):
            current.append(min(current[-1]+1, previous[i]+1, previous[i-1]+(a != b)))
        previous = current
    return 1 - previous[-1] / max(len(left), len(right))

