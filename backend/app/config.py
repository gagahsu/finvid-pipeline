from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    database_url: str = "sqlite:///./finvid.db"

    class Config:
        env_file = ".env"


settings = Settings()
