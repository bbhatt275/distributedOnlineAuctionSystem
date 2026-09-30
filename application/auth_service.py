import secrets

import bcrypt

class AuthService:

    def __init__(self):
        # username -> password hash
        self.users = {}

        # token -> username
        self.active_tokens = {}

        # Create users for M1 testing

        self.create_user("ashish", "ashish123")
        self.create_user("bharat", "password123")
        self.create_user("chetna", "chetna123")
        self.create_user("devashish", "devashish123")
        self.create_user("shivesh", "shivesh123")

    def create_user(self, username, password):
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()
        )

        self.users[username] = password_hash

    def login(self, username, password):
        if username not in self.users:
            return None

        password_hash = self.users[username]

        if not bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash
        ):
            return None

        token = secrets.token_hex(32)

        self.active_tokens[token] = username

        return token

    def logout(self, token):
        if token not in self.active_tokens:
            return False

        del self.active_tokens[token]

        return True

    def validate_token(self, token):
        return self.active_tokens.get(token)