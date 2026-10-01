select * from {{ ref('stg_spend') }}
where invoice_id = '' or supplier_id = ''
   or not regexp_full_match(currency, '[A-Z]{3}')
   or not regexp_full_match(amount, '-?[0-9]{1,12}(\.[0-9]{1,2})?')
   or not regexp_full_match(invoice_date, '[0-9]{4}-[0-9]{2}-[0-9]{2}')
   or try_cast(invoice_date as date) is null
