"""Read-only vault secret references; no values in config, CLI args or audit."""
import json
import os
import re
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, ProxyHandler
from .explanations import NoRedirect


class SecretStore:
    def __init__(self, config):
        self.config = config
        provider = config.get('provider')
        if provider not in {'hashicorp', 'azure'}: raise ValueError('Choose hashicorp or azure secret provider')
        url = config.get('url', '')
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/') or not parsed.hostname:
            raise ValueError('Invalid vault URL')
        if parsed.scheme != 'https' and not (provider=='hashicorp' and parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost'}):
            raise ValueError('Remote vault connections require verified HTTPS')
        if provider=='azure' and not any(parsed.hostname.endswith('.'+domain) for domain in ('vault.azure.net','vault.usgovcloudapi.net','vault.azure.cn')):
            raise ValueError('Use a verified Azure Key Vault cloud hostname')
        self.url = url.rstrip('/')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.client = None

    def get(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_/-]{1,200}', reference) or any(p in {'', '.', '..'} for p in reference.split('/')):
            raise ValueError('Invalid secret reference')
        try:
            if self.config['provider']=='azure':
                if '/' in reference: raise ValueError('Azure secret names cannot include paths')
                if self.client is None:
                    from azure.identity import DefaultAzureCredential
                    from azure.keyvault.secrets import SecretClient
                    self.client = SecretClient(vault_url=self.url, credential=DefaultAzureCredential(), retry_total=0, connection_timeout=5, read_timeout=5)
                value = self.client.get_secret(reference).value
            else:
                mount = self.config.get('mount','secret')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', mount): raise ValueError('Invalid KV v2 mount')
                token = os.environ.get(self.config.get('token_env','OUTCOME_VAULT_TOKEN'), '')
                if not token: raise ValueError('Vault authentication unavailable')
                request = Request(self.url+'/v1/'+mount+'/data/'+quote(reference, safe='/'), headers={'X-Vault-Token':token})
                with self.opener.open(request, timeout=5) as response:
                    body = response.read(65537)
                if len(body)>65536: raise ValueError('Vault response exceeded bound')
                value = json.loads(body)['data']['data']['value']
            if not isinstance(value, str) or not value or len(value)>32768: raise ValueError('Invalid secret value')
            return value
        except Exception:
            # Do not expose URLs, auth headers, response bodies or SDK errors.
            raise ValueError('Configured vault secret unavailable; no local fallback') from None


def load_store(path):
    from pathlib import Path
    return SecretStore(json.loads(Path(path).read_text(encoding='utf-8')))
