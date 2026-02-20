# Licensing

Oduflow is source-available under the [Polyform Noncommercial License 1.0.0](https://github.com/oduist/oduflow/blob/main/LICENSE). Commercial use requires a license.

## License Types

| Type | Label | Description |
|---|---|---|
| `unlicensed` | UNLICENSED — NON-COMMERCIAL USE ONLY | Default when no license key is installed |
| `individual` | Licensed to individual | Personal commercial license |
| `business` | Licensed to company (internal use only) | Company license for internal use |
| `integrator` | Licensed to Odoo integrator | License for Odoo integrators and consultancies |

## Installing a License

**Via CLI (during init):**

```bash
oduflow init --license /path/to/license.key
```

**Via CLI (standalone):**

The license file is stored at `/etc/oduflow/license.key`.

**Via Web Dashboard:**

Navigate to the dashboard and use the license activation form. The license key text can be pasted directly.

**Via REST API:**

```bash
curl -X POST http://localhost:8000/api/license/activate \
  -H "Content-Type: application/json" \
  -d '{"key": "<license-key-text>"}'
```

## Checking License Status

```bash
# Via REST API
curl http://localhost:8000/api/license

# Via Web Dashboard — license info is displayed in the dashboard header
```

License keys are RSA-signed and verified against a built-in public key. Invalid or tampered keys are rejected.

---

For business use or integrator licenses, visit [oduflow.dev](https://oduflow.dev).
