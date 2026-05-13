from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://agent-redis:6379/0"
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""  # Comma-separated user IDs
    log_level: str = "INFO"

    # Redis stream names (must match agent-core)
    stream_incoming: str = "events:incoming"
    stream_outgoing: str = "events:outgoing"
    consumer_group: str = "agent-telegram-group"
    consumer_name: str = "telegram-1"

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.telegram_allowed_users:
            return set()
        return {int(uid.strip()) for uid in self.telegram_allowed_users.split(",") if uid.strip()}


settings = Settings()
