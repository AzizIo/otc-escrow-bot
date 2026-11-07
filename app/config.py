from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    superadmin_id: int = 7620642308
    currency: str = "USDT"
    crypto_pay_api_token: str | None = None
    crypto_pay_api_base: str = "https://pay.crypt.bot/api"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
