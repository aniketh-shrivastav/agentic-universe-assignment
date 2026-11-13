from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def normalize_header(header: str) -> str:
    """
    Normalise a header string:
    lower-case it, trim it, and replace every run of '_' or whitespace with a single space.
    """
    h = header.lower().strip()
    return re.sub(r'[\s_]+', ' ', h)


@dataclass
class FieldDef:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: List[str] = field(default_factory=list)
    values: Optional[List[str]] = None
    default: Optional[str] = None


@dataclass
class SchemaDef:
    workflow_id: str
    description: str
    fields: List[FieldDef]

    def __post_init__(self):
        # Build normalized alias lookup map -> FieldDef
        self._header_to_field: Dict[str, FieldDef] = {}
        for f in self.fields:
            # Map canonical name
            self._header_to_field[normalize_header(f.name)] = f
            # Map all aliases
            for alias in f.aliases:
                self._header_to_field[normalize_header(alias)] = f

    def match_header(self, raw_header: str) -> Optional[FieldDef]:
        """
        Matches a raw CSV header against field name or aliases.
        Returns FieldDef if matched, otherwise None (ignoring columns that match nothing).
        """
        norm = normalize_header(raw_header)
        return self._header_to_field.get(norm)

    def get_reference_field(self) -> Optional[FieldDef]:
        for f in self.fields:
            if f.reference:
                return f
        return None


@dataclass
class DispositionDef:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class SchemaRegistry:
    def __init__(self, schemas_dir: Optional[Path | str] = None):
        self.schemas: Dict[str, SchemaDef] = {}
        self.dispositions: Dict[str, Dict[str, DispositionDef]] = {}
        if schemas_dir is not None:
            self.load_all(Path(schemas_dir))

    def load_all(self, schemas_dir: Path):
        # Load schema json files
        for json_file in schemas_dir.glob("*.json"):
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                fields = [
                    FieldDef(
                        name=fld["name"],
                        type=fld["type"],
                        required=fld.get("required", False),
                        pii=fld.get("pii", False),
                        reference=fld.get("reference", False),
                        aliases=fld.get("aliases", []),
                        values=fld.get("values"),
                        default=fld.get("default"),
                    )
                    for fld in data["fields"]
                ]
                schema = SchemaDef(
                    workflow_id=data["workflow_id"],
                    description=data.get("description", ""),
                    fields=fields,
                )
                self.schemas[schema.workflow_id] = schema

        # Load dispositions.csv
        disp_file = schemas_dir / "dispositions.csv"
        if disp_file.exists():
            with open(disp_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    disp = DispositionDef(
                        workflow_id=wf,
                        code=code,
                        label=row["label"].strip(),
                        is_terminal=is_term,
                        is_success=is_succ,
                    )
                    if wf not in self.dispositions:
                        self.dispositions[wf] = {}
                    self.dispositions[wf][code] = disp

    def get_schema(self, workflow_id: str) -> Optional[SchemaDef]:
        return self.schemas.get(workflow_id)

    def get_disposition(self, workflow_id: str, code: str) -> Optional[DispositionDef]:
        return self.dispositions.get(workflow_id, {}).get(code)
