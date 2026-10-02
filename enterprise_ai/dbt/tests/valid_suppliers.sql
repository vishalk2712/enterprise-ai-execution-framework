select * from {{ ref('stg_suppliers') }}
where supplier_id = '' or name = '' or not regexp_full_match(country, '[A-Z]{2}')
   or (lei <> '' and not regexp_full_match(lei, '[A-Z0-9]{18}[0-9]{2}'))
   or (parent_lei <> '' and not regexp_full_match(parent_lei, '[A-Z0-9]{18}[0-9]{2}'))
   or (lei <> '' and lei = parent_lei)
   or (bank_account_hash <> '' and not regexp_full_match(bank_account_hash, '[0-9a-f]{64}'))
