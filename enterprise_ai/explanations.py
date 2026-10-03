"""Two-sentence evidence rendering; optional local models select facts, not prose."""
import json
import re
import time
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


def explain_group(entity, records, model=None, config=None):
    """A local model selects a factual summary plan; trusted code renders prose.

    Supplier labels never enter the model prompt. Output cannot add new claims.
    """
    from .governance import validate_cluster, direct_identity
    from .matching import score_pair, MatchConfig
    from .normalization import identifier
    from .mock_erp import fingerprint
    if not records or sorted(r['supplier_id'] for r in records) != sorted(entity['source_supplier_ids']):
        raise ValueError('Rationale membership differs from the resolved group')
    labels = ', '.join(json.dumps(r['name'][:120], ensure_ascii=False) for r in records[:3])
    if len(records) > 3:
        labels += f' and {len(records)-3} more source labels'
    if len(records) == 1:
        return {'entity_id': entity['entity_id'], 'text': f'Source label {labels} remains a single supplier record. No supplier merge or external write was performed by this resolution.',
                'backend': 'deterministic_evidence', 'model_used': False, 'version': 'group-rationale-v1'}
    checked = validate_cluster(records)
    if not checked['valid']:
        raise ValueError('Cannot explain an invalid legal identity group as merged')
    a, b = records[:2]
    keys = direct_identity(a, b)
    proof = ' and '.join(('registration ID' if k == 'registration_id' else 'LEI') + ' ' + json.dumps(identifier(a[k])) for k in keys)
    # Report only a recorded feature of this explicit pair, not a group minimum.
    feature = score_pair(a, b, config or MatchConfig())['features']
    facts = {'authority': f'The first source pair shares supplied {proof}; all {checked["pair_checks"]} group pair checks passed direct legal identity and conflict rules',
             'name': f'Name similarity for {a["supplier_id"]} / {b["supplier_id"]} is {feature["name_levenshtein"]:.3f}',
             'policy': 'Shared VAT, bank hashes and postcodes alone do not establish legal identity'}
    if feature.get('address_jaccard') is not None:
        facts['address'] = f'Address token overlap for {a["supplier_id"]} / {b["supplier_id"]} is {feature["address_jaccard"]:.3f}'
    selected = ['authority', 'name']
    result = {'entity_id': entity['entity_id'], 'backend': 'deterministic_evidence', 'model_used': False, 'version': 'group-rationale-v1',
              'evidence_hash': fingerprint(facts), 'group_checks': checked['pair_checks']}
    if model is not None:
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,100}', model) or 'cloud' in model.lower():
            raise ValueError('Use an already installed local Ollama model')
        schema = {'type': 'object', 'properties': {'fact_ids': {'type': 'array', 'items': {'type': 'string', 'enum': list(facts)}, 'minItems': 2, 'maxItems': 2}}, 'required': ['fact_ids'], 'additionalProperties': False}
        started = time.monotonic()
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            show = Request('http://127.0.0.1:11434/api/show', json.dumps({'model': model}).encode(), {'Content-Type': 'application/json'})
            with opener.open(show, timeout=5) as response:
                body = response.read(262145)
            if len(body) > 262144:
                raise ValueError('Metadata exceeded bound')
            info = json.loads(body)
            if info.get('remote_host') or info.get('remote_model') or not info.get('model_info') or info.get('details', {}).get('format') != 'gguf':
                raise ValueError('Installed local GGUF model required')
            result['model_metadata_hash'] = fingerprint(info.get('model_info'))
            payload = {'model': model, 'stream': False, 'format': schema, 'options': {'temperature': 0, 'num_predict': 80, 'num_ctx': 2048},
                       'prompt': 'Summarize a recorded supplier group using only these facts. Select authority and ONE most useful supporting fact. Return only fact_ids JSON. Do not decide or approve anything. Facts: ' + json.dumps(facts)}
            generate = Request('http://127.0.0.1:11434/api/generate', json.dumps(payload).encode(), {'Content-Type': 'application/json'})
            with opener.open(generate, timeout=30) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise ValueError('Response exceeded bound')
            plan = json.loads(json.loads(body)['response'])
            if (not isinstance(plan, dict) or set(plan) != {'fact_ids'} or not isinstance(plan['fact_ids'], list) or len(plan['fact_ids']) != 2
                    or any(not isinstance(k, str) or k not in facts for k in plan['fact_ids']) or len(set(plan['fact_ids'])) != 2 or 'authority' not in plan['fact_ids']):
                raise ValueError('Generated plan added or omitted evidence')
            selected = ['authority', next(k for k in plan['fact_ids'] if k != 'authority')]
            result.update(backend='ollama_fact_selection', model_used=True, model=model, latency_ms=round((time.monotonic()-started)*1000))
        except (ValueError, KeyError, TypeError, OSError):
            result['fallback'] = 'Local model unavailable or failed evidence validation'
    result['fact_ids'] = selected
    result['text'] = f'Grouped source labels {labels}: {facts[selected[0]]}. {facts[selected[1]]}; external execution requires a separately approved payload.'
    return result
