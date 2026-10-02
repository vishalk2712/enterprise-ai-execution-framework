"""Two-sentence evidence rendering; optional local models select facts, not prose."""
import json
import re
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Audit inference redirects are disabled")


def facts_for(item):
    decision = item.get("operational_decision", {})
    features = item["features"]
    facts = {"name": f"Recorded name similarity is {features['name_levenshtein']:.3f}",
             "outcome": "Recorded algorithmic outcome is " + item["algorithmic_outcome"],
             "tier": "Operational tier is " + decision.get("tier", "Uncalibrated_Diagnostic")}
    if features.get("address_jaccard") is not None:
        facts["address"] = f"Recorded address token overlap is {features['address_jaccard']:.3f}"
    if features.get("identifier_conflict"):
        facts["conflict"] = "Recorded evidence contains an identity conflict"
    if item.get("authority_grouped"):
        facts["clique"] = "Every pair in the resolved group shares a direct legal identity key without a recorded conflict"
    if item.get("probability_status") == "calibrated_pair_estimate":
        facts["probability"] = f"The calibrated candidate-pair estimate is {item['match_probability']:.4f}"
    return facts


def render(item, selection=None):
    facts = facts_for(item)
    required = "conflict" if "conflict" in facts else "clique" if "clique" in facts else "name"
    selected = [required, "tier"]
    if selection is not None:
        if (not isinstance(selection, dict) or set(selection) != {"fact_ids"}
                or not isinstance(selection["fact_ids"], list) or len(selection["fact_ids"]) != 2
                or any(not isinstance(k, str) or k not in facts for k in selection["fact_ids"])
                or len(set(selection["fact_ids"])) != 2 or required not in selection["fact_ids"]
                or "tier" not in selection["fact_ids"]):
            raise ValueError("Audit model must select exactly the required evidence and tier facts")
        selected = selection["fact_ids"]
    # Source names, addresses and generated free text are never interpolated.
    return facts[selected[0]] + ". " + facts[selected[1]] + "; this explanation does not approve a merge or ledger action."


def explain(item, model=None):
    result = {"evaluation_id": item.get("evaluation_id"), "text": render(item), "backend": "deterministic_evidence", "model_used": False}
    if model is None:
        return result
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,100}", model) or "cloud" in model.lower():
        raise ValueError("Use the name of an already installed local Ollama model")
    schema = {"type": "object", "properties": {"fact_ids": {"type": "array", "items": {"type": "string", "enum": list(facts_for(item))}, "minItems": 2, "maxItems": 2}}, "required": ["fact_ids"], "additionalProperties": False}
    payload = {"model": model, "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 80, "num_ctx": 2048},
               "prompt": "Select exactly the required evidence fact (conflict, else clique, else name) and tier. Return only fact_ids JSON. Facts: " + json.dumps(facts_for(item))}
    try:
        # Fixed loopback endpoint, no proxies, redirects, model pulls or tools.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        info_request = Request("http://127.0.0.1:11434/api/show", json.dumps({"model": model}).encode(), {"Content-Type": "application/json"})
        with opener.open(info_request, timeout=5) as response:
            info_body = response.read(262145)
        if len(info_body) > 262144:
            raise ValueError("Model metadata exceeded bound")
        info = json.loads(info_body)
        if (info.get("remote_host") or info.get("remote_model") or not info.get("model_info")
                or info.get("details", {}).get("format") != "gguf"):
            raise ValueError("Only installed local GGUF models are accepted")
        request = Request("http://127.0.0.1:11434/api/generate", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("Audit inference response too large")
        generated = json.loads(body)
        result.update(text=render(item, json.loads(generated["response"])), backend="ollama_fact_selection", model_used=True, model=model)
    except (ValueError, KeyError, TypeError, OSError):
        result["fallback"] = "Local model unavailable or failed evidence validation"
    return result
