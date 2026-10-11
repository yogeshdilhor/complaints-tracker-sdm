from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change.html",
            success_url=reverse_lazy("password_changed"),
        ),
        name="password_change",
    ),
    path(
        "password/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_changed.html"
        ),
        name="password_changed",
    ),
    path("", include("complaints.urls")),
]
