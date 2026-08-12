from src.storage import UserRepository


class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    def display_name(self, user_id: str) -> str:
        user = self.repository.get_user(user_id)
        return f"{user.first_name} {user.last_name}"
