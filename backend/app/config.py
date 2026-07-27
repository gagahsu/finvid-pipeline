from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    database_url: str = "sqlite:///./finvid.db"

    class Config:
        env_file = ".env"


settings = Settings()
