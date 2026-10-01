-- Exact duplicate rows are permitted and deduplicated by the engine with a warning.
select invoice_id from (
  select distinct invoice_id, supplier_id, invoice_date, amount, currency, category, description
  from {{ ref('stg_spend') }}
) unique_rows group by invoice_id having count(*) > 1
