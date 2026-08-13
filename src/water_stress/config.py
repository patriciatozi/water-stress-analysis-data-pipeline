from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectSettings(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    timezone: str = Field(min_length=1)


class StudySettings(BaseModel):
    municipality_name: str = Field(min_length=1)
    municipality_code: str
    start_date: date
    end_date: date

    @field_validator("municipality_code")
    @classmethod
    def validate_municipality_code(cls, value: str) -> str:
        if len(value) != 7 or not value.isdigit():
            raise ValueError("municipality_code must contain exactly 7 digits")
        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> StudySettings:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class IbgeSettings(BaseModel):
    base_url: HttpUrl
    quality: str = Field(min_length=1)


class NasaPowerSettings(BaseModel):
    base_url: HttpUrl
    community: str = Field(min_length=1)
    time_standard: str = Field(min_length=1)
    parameters: list[str] = Field(min_length=1)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("NASA POWER parameters must be unique")
        if any(not parameter.strip() for parameter in value):
            raise ValueError("NASA POWER parameters cannot be blank")
        return value


class SoilGridsSettings(BaseModel):
    base_url: HttpUrl
    service: str = "WCS"
    version: str = "2.0.1"
    format: str = "GEOTIFF_INT16"
    source_crs: str = "EPSG:4326"
    subset_crs: str = "ESRI:54052"
    quantile: str = "Q0.5"
    properties: list[str] = Field(min_length=1)
    depths: list[str] = Field(min_length=1)

    @field_validator("properties", "depths")
    @classmethod
    def validate_unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("SoilGrids values must be unique and non-blank")
        return value


class Sentinel2Settings(BaseModel):
    search_url: HttpUrl
    collection: str = Field(min_length=1)
    max_cloud_cover: float = Field(ge=0, le=100)
    representative_scenes: int = Field(ge=1)
    page_limit: int = Field(ge=1, le=1000)
    assets: list[str] = Field(min_length=1)

    @field_validator("assets")
    @classmethod
    def validate_assets(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("Sentinel-2 assets must be unique and non-blank")
        return value


class HttpSettings(BaseModel):
    timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1)
    backoff_seconds: float = Field(ge=0)


class StorageSettings(BaseModel):
    root_path: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATER_STRESS_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    project: ProjectSettings
    study: StudySettings
    ibge: IbgeSettings
    nasa_power: NasaPowerSettings
    soilgrids: SoilGridsSettings
    sentinel_2: Sentinel2Settings
    http: HttpSettings
    storage: StorageSettings
    config_hash: str = Field(exclude=True)


def load_settings(path: Path = Path("configs/project.yml")) -> Settings:
    raw = path.read_bytes()
    parsed: Any = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"Configuration at {path} must be a YAML mapping")
    parsed["config_hash"] = hashlib.sha256(raw).hexdigest()
    return Settings.model_validate(parsed)
