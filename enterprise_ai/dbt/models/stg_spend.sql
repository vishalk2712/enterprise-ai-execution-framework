select trim(invoice_id) as invoice_id, trim(supplier_id) as supplier_id,
       trim(invoice_date) as invoice_date, trim(amount) as amount,
       upper(trim(currency)) as currency, trim(category) as category,
       trim(description) as description
from {{ source('intake', 'spend') }}
