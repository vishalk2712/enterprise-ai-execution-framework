"""Versioned, shared normalization. No learned or external transformations."""
import re
import unicodedata

VERSION = "unicode-legal-suffix-v2"


def tokens(value):
    value = unicodedata.normalize("NFKD", str(value).casefold())
    return re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c)))


def name_key(value):
    return " ".join(w for w in tokens(value) if w not in {"limited", "ltd", "inc", "llc", "gmbh", "plc"})


def identifier(value):
    normalized = re.sub(r"[\s.\-/]", "", str(value)).upper()
    return "" if normalized in {"NA", "NIL", "NONE", "NULL", "UNKNOWN", "NOTAVAILABLE", "NOTAPPLICABLE", "MISSING", "TBD"} else normalized


def soundex(value):
    """English Soundex; no invented transliteration for non-Latin names."""
    letters = "".join(c for c in name_key(value).upper() if "A" <= c <= "Z")
    if not letters:
        return ""
    codes = {c: str(i) for i, group in enumerate(("BFPV", "CGJKQSXZ", "DT", "L", "MN", "R"), 1) for c in group}
    result, previous = letters[0], codes.get(letters[0], "")
    for c in letters[1:]:
        if c in "HW":
            continue
        code = codes.get(c, "")
        if code and code != previous:
            result += code
        previous = code
    return (result + "000")[:4]


def ngrams(value, n=3):
    value = " " + name_key(value) + " "
    return {value[i:i+n] for i in range(max(0, len(value)-n+1))} if value.strip() else set()
