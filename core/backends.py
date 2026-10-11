from django.contrib.auth.backends import ModelBackend


class ExpiringModelBackend(ModelBackend):
    """A login whose expiry date has passed can no longer sign in."""

    def user_can_authenticate(self, user) -> bool:
        return super().user_can_authenticate(user) and not user.is_login_expired
