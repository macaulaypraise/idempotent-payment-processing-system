from fastapi import Depends
from functools import lru_cache
from app.config import Settings, get_settings

def get_settings_dep(settings: Settings = Depends(get_settings)) -> Settings:
    return settings
