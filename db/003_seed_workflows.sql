-- =============================================================================
-- Example workflow registration. Add or update more workflows through
-- PUT /v1/admin/workflows/{name} on the Integration Layer API.
-- =============================================================================

INSERT INTO integration.workflow_registry
    (workflow_name, target_app, description, max_retries, input_schema, output_schema)
VALUES (
    'order_validation',
    'app2',
    'App1 submits an order; App2 validates it and returns an approval decision.',
    3,
    $json${
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "additionalProperties": false,
      "required": ["order_id", "customer_id", "currency", "items"],
      "properties": {
        "order_id":    {"type": "string", "minLength": 1, "maxLength": 64},
        "customer_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "currency":    {"type": "string", "pattern": "^[A-Z]{3}$"},
        "order_date":  {"type": "string", "format": "date"},
        "items": {
          "type": "array", "minItems": 1, "maxItems": 500,
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["sku", "quantity", "unit_price"],
            "properties": {
              "sku":        {"type": "string", "minLength": 1},
              "quantity":   {"type": "integer", "minimum": 1},
              "unit_price": {"type": "number", "minimum": 0}
            }
          }
        }
      }
    }$json$::jsonb,
    $json${
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "required": ["order_id", "decision", "total_amount", "currency"],
      "properties": {
        "order_id":     {"type": "string"},
        "decision":     {"enum": ["APPROVED", "REJECTED"]},
        "total_amount": {"type": "number", "minimum": 0},
        "currency":     {"type": "string", "pattern": "^[A-Z]{3}$"},
        "reasons":      {"type": "array", "items": {"type": "string"}}
      }
    }$json$::jsonb
)
ON CONFLICT (workflow_name) DO NOTHING;
