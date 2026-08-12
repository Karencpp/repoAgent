from src.service import UserService
from src.storage import User, UserRepository


def test_display_name():
    repository = UserRepository(
        {"u1": User(user_id="u1", first_name="Ada", last_name="Lovelace")}
    )
    assert UserService(repository).display_name("u1") == "Ada Lovelace"
