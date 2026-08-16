import auth_config
from token_utils import generate_token

def login(username, password):
    # TODO: Implement actual authentication logic
    if username == auth_config.ADMIN_USERNAME and password == auth_config.ADMIN_PASSWORD:
        return generate_token(username)
    return None
