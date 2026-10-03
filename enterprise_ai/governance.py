"""Legal-identity and probability policies; scores never authorize ledger writes."""
from itertools import combinations
import math

from .normalization import identifier

REVIEW_FLOOR = .75
AUTO_FLOOR = .99
VERSION = "corporate-governance-v2"

# Placeholders that survive identifier() because they are alphanumeric.
PLACEHOLDER_IDS = {"TBC", "TBA", "PENDING", "SAME", "ASABOVE", "DUMMY", "TEMP",
                   "TEST", "VARIOUS", "MISC", "SUNDRY", "ONEOFF", "NOTREG",
                   "NOTREGISTERED", "NOREG", "NONREG", "REFER", "SEEABOVE"}


def degenerate_identifier(value, min_length=0):
    """True when an authority ID cannot carry legal identity on its own.

    Covers placeholders operators type into mandatory fields: 0, 1, X, ----,
    999999, TBC. These are not identity and must never drive an auto-merge.
    min_length is a policy knob: 0 disables the length rule, 5 rejects any ID
    shorter than the shortest real Companies House / VAT / LEI identifier.
    """
    value = identifier(value)
    if not value:
        return False
    if value in PLACEHOLDER_IDS:
        return True
    if len(set(value)) == 1:
        return True
    return min_length > 0 and len(value) < min_length


def valid_lei(value):
    if len(value) != 20 or not value.isascii() or not value.isalnum() or not value[-2:].isdigit():
        return False
    expanded = "".join(str(ord(c)-55) if c.isalpha() else c for c in value.upper())
    return int(expanded) % 97 == 1


def conflicts(left, right):
    reasons = []
    if left.get("country") != right.get("country"):
        reasons.append("country_disagreement")
    for field in ("registration_id", "tax_id", "lei", "bank_account_hash"):
        a, b = identifier(left.get(field, "")), identifier(right.get(field, ""))
        if field in {"registration_id", "tax_id"}:
            a = "" if degenerate_identifier(a) else a
            b = "" if degenerate_identifier(b) else b
        if a and b and a != b:
            reasons.append(field + "_disagreement")
    a, b = identifier(left.get("lei", "")), identifier(right.get("lei", ""))
    if (a and a == identifier(right.get("parent_lei", ""))) or (b and b == identifier(left.get("parent_lei", ""))):
        reasons.append("parent_child_relationship")
    return reasons


def direct_identity(left, right):
    # Shared VAT groups, postal offices and accounts can span legal entities.
    return [field for field in ("registration_id", "lei")
            if identifier(left.get(field, "")) and not degenerate_identifier(left.get(field, ""))
            and identifier(left.get(field, "")) == identifier(right.get(field, ""))]


def validate_cluster(records):
    """Require a consistent clique, not just a connected path of weak matches."""
    for left, right in combinations(sorted(records, key=lambda r: r["supplier_id"]), 2):
        reasons = conflicts(left, right)
        if not direct_identity(left, right):
            reasons.append("no_direct_legal_identity_key")
        if reasons:
            return {"valid": False, "left_id": left["supplier_id"], "right_id": right["supplier_id"], "reasons": reasons}
    return {"valid": True, "pair_checks": len(records)*(len(records)-1)//2}


def operational_tier(evaluation, left, right):
    reasons = conflicts(left, right)
    if reasons:
        return {"tier": "Conflict_Review", "reasons": reasons, "auto_merge_eligible": False}
    probability = evaluation.get("match_probability")
    calibrated = evaluation.get("probability_status") == "calibrated_pair_estimate"
    if not calibrated or isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
        return {"tier": "Uncalibrated_Review" if evaluation["algorithmic_outcome"] == "Review_Candidate" else "Uncalibrated_Diagnostic",
                "reasons": ["No independently calibrated pair model; probability thresholds do not apply"], "auto_merge_eligible": False}
    if probability < REVIEW_FLOOR:
        return {"tier": "Separate_Diagnostic", "reasons": ["Below the calibrated review floor"], "auto_merge_eligible": False}
    if probability >= AUTO_FLOOR and direct_identity(left, right):
        return {"tier": "Exact_Identity_Eligible", "reasons": ["Calibrated estimate >=0.99 and a direct legal identity key; cluster validation still required"],
                "auto_merge_eligible": True}
    return {"tier": "Human_Review", "reasons": ["Review calibrated candidate; strong score alone is insufficient for legal identity"], "auto_merge_eligible": False}


def annotate(evaluations, records):
    by_id = {r["supplier_id"]: r for r in records}
    for item in evaluations:
        item["operational_policy"] = VERSION
        item["operational_decision"] = operational_tier(item, by_id[item["left_id"]], by_id[item["right_id"]])
        if item["algorithmic_outcome"] == "Excluded_Sampled":
            item["operational_decision"] = {"tier": "Excluded_Diagnostic", "reasons": ["Sampled blocking miss retained for review; not a negative label"], "auto_merge_eligible": False}
        elif item["operational_decision"]["tier"] == "Conflict_Review":
            item["algorithmic_outcome"] = "Conflict_Review"
        elif item["probability_status"] == "calibrated_pair_estimate":
            item["algorithmic_outcome"] = "Below_Threshold" if item["operational_decision"]["tier"] == "Separate_Diagnostic" else "Review_Candidate"
