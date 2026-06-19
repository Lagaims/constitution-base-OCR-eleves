from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class StudentEntry:
    student_id: int
    academy: str
    level: str


@dataclass(frozen=True)
class ScanInfo:
    student_id: int
    academy: str
    level: str
    filename: str
    source_url: str


@dataclass(frozen=True)
class ScanRecord:
    filename: str
    student_id: int
    level: str
    academy: str
    s3_path: str

    def to_dict(self) -> dict:
        return asdict(self)
