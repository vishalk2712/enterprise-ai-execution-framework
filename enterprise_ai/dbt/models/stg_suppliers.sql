select trim(supplier_id) as supplier_id,
       trim(regexp_replace(name, '\s+', ' ', 'g')) as name,
       upper(trim(country)) as country,
       trim(registration_id) as registration_id,
       trim(tax_id) as tax_id,
       upper(trim(postcode)) as postcode,
       trim(regexp_replace(address, '\s+', ' ', 'g')) as address,
       trim(aliases) as aliases,
       upper(trim(lei)) as lei,
       upper(trim(parent_lei)) as parent_lei,
       lower(trim(bank_account_hash)) as bank_account_hash
from {{ source('intake', 'suppliers') }}
