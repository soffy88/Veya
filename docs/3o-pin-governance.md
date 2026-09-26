# 3O Pin Governance

Canonical source: `platform/3O/CANONICAL_PINS.json`

Production restart: `python scripts/production_backend.py restart`

Never: direct `docker compose restart backend` / `docker compose up -d backend`

 Pin update protocol: qualify → manifest → gitlink → governance → P10 → restart

- `scripts/check_3o_canonical_pins.py` blocks uncoordinated gitlink changes.
- `scripts/production_backend_preflight.py` blocks unsafe restarts.
- Neither tool repairs anything; reconcile owner sessions by hand.
