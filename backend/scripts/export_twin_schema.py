"""Writes docs/api/twin-v2.schema.json, the contract with the 3D frontend."""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.digital_twin import schemas  # noqa: E402

out = BACKEND.parent / "docs" / "api" / "twin-v2.schema.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({m.__name__: m.model_json_schema()
                           for m in (schemas.TwinView, schemas.TwinMessage)}, indent=2) + "\n",
               encoding="utf-8", newline="\n")
print(f"wrote {out.name}")
