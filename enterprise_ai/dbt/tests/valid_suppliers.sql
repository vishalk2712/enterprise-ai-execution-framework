select * from {{ ref('stg_suppliers') }}
where supplier_id = '' or name = '' or not regexp_full_match(country, '[A-Z]{2}')
