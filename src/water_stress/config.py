from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectSettings(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    timezone: str = Field(min_length=1)


class StudySettings(BaseModel):
    area_type: Literal["state", "municipality"]
    area_name: str = Field(min_length=1)
    area_code: str
    start_date: date
    end_date: date

    @field_validator("area_code")
    @classmethod
    def validate_area_code(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("area_code must contain only digits")
        return value

    @model_validator(mode="after")
    def validate_study(self) -> StudySettings:
        expected_length = 2 if self.area_type == "state" else 7
        if len(self.area_code) != expected_length:
            raise ValueError(
                f"area_code must contain exactly {expected_length} digits for {self.area_type}"
            )
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self

    @property
    def partition_key(self) -> str:
        return f"{self.area_type}_code={self.area_code}"


class IbgeSettings(BaseModel):
    base_url: HttpUrl
    quality: str = Field(min_length=1)


class NasaPowerSettings(BaseModel):
    point_url: HttpUrl
    regional_url: HttpUrl
    community: str = Field(min_length=1)
    time_standard: str = Field(min_length=1)
    parameters: list[str] = Field(min_length=1)
    regional_chunk_degrees: float = Field(ge=2, le=10)

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
    chunk_size_meters: int = Field(gt=0)

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
    max_tiles_per_month: int = Field(default=10, ge=1)
    max_workers: int = Field(default=2, ge=1, le=4)
    page_limit: int = Field(ge=1, le=1000)
    assets: list[str] = Field(min_length=1)
    download_bronze_assets: bool = False

    @field_validator("assets")
    @classmethod
    def validate_assets(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("Sentinel-2 assets must be unique and non-blank")
        return value


class MapBiomasSettings(BaseModel):
    base_url: HttpUrl
    collection: int = Field(ge=1)
    reference_year: int = Field(ge=1985, le=2100)
    soybean_class: int = Field(ge=1)
    license: str = Field(min_length=1)


class SpatialArchitectureSettings(BaseModel):
    query_crs: str = "EPSG:4326"
    area_crs: str = "EPSG:5880"
    screening_grid_meters: int = Field(gt=0)
    detail_grid_meters: int = Field(gt=0)
    indicator_window_days: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_grid_hierarchy(self) -> SpatialArchitectureSettings:
        if self.detail_grid_meters >= self.screening_grid_meters:
            raise ValueError("detail grid must be finer than screening grid")
        if self.screening_grid_meters % self.detail_grid_meters != 0:
            raise ValueError("screening grid must be divisible by detail grid")
        return self


class GoldSettings(BaseModel):
    soy_fraction_threshold: float = Field(default=0.25, ge=0, le=1)
    satellite_max_age_days: int = Field(default=30, gt=0)
    deficit_reference_mm: float = Field(default=20.0, gt=0)
    ndvi_stress_threshold: float = Field(default=0.7, ge=-1, le=1)
    ndmi_stress_threshold: float = Field(default=0.2, ge=-1, le=1)
    deficit_weight: float = Field(default=0.5, ge=0, le=1)
    ndvi_weight: float = Field(default=0.3, ge=0, le=1)
    ndmi_weight: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def validate_score_weights(self) -> GoldSettings:
        if self.deficit_weight + self.ndvi_weight + self.ndmi_weight <= 0:
            raise ValueError("At least one Gold score weight must be positive")
        return self


class HttpSettings(BaseModel):
    timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1)
    backoff_seconds: float = Field(ge=0)


class StorageSettings(BaseModel):
    root_path: Path
    silver_root_path: Path = Path("data/silver")
    gold_root_path: Path = Path("data/gold")


class DatabaseSettings(BaseModel):
    enabled: bool = False
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr | None = None
    sslmode: str = Field(default="prefer", min_length=1)
    connect_timeout_seconds: int = Field(default=10, gt=0)


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
    mapbiomas: MapBiomasSettings
    spatial: SpatialArchitectureSettings
    gold: GoldSettings
    http: HttpSettings
    storage: StorageSettings
    database: DatabaseSettings
    config_hash: str = Field(exclude=True)


def load_settings(path: Path = Path("configs/project.yml")) -> Settings:
    raw = path.read_bytes()
    parsed: Any = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"Configuration at {path} must be a YAML mapping")
    for section, values in parsed.items():
        if not isinstance(values, dict):
            continue
        for field_name in values:
            environment_name = f"WATER_STRESS_{section.upper()}__{field_name.upper()}"
            raw_value = os.getenv(environment_name)
            if raw_value is not None:
                current_value = values[field_name]
                # Preserve textual environment values. Otherwise a numeric password or
                # area code would be converted to an integer by YAML before validation.
                values[field_name] = (
                    raw_value
                    if isinstance(current_value, str) or current_value is None
                    else yaml.safe_load(raw_value)
                )
    parsed["config_hash"] = hashlib.sha256(raw).hexdigest()
    return Settings.model_validate(parsed)
