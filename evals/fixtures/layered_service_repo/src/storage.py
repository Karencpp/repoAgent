from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    user_id: str
    first_name: str
    last_name: str


class UserRepository:
    def __init__(self, users: dict[str, User]) -> None:
        self.users = users

    def get_user(self, user_id: str) -> User:
        return self.users[user_id]
