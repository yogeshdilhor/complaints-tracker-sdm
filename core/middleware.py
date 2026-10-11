from django.contrib.auth import logout
from django.shortcuts import redirect


class ExpiredLoginMiddleware:
    """Signs out someone whose login expired while they were already signed in."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_login_expired:
            logout(request)
            if request.headers.get("HX-Request"):
                response = redirect("login")
                response["HX-Redirect"] = response["Location"]
                return response
            return redirect("login")
        return self.get_response(request)
