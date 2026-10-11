from django.urls import path

from . import views

app_name = "complaints"

urlpatterns = [
    path("", views.complaint_list, name="list"),
    path("c/<int:pk>/", views.complaint_detail, name="detail"),
    path("c/<int:pk>/do/<str:action>/", views.complaint_action, name="action"),
    path("c/<int:pk>/stage-from-document/", views.stage_from_document, name="stage_from_document"),
    path("documents/<int:pk>/", views.document_download, name="document"),
    path("documents/<int:pk>/summary/", views.document_summary, name="document_summary"),
    path("new/", views.intake_upload, name="intake_upload"),
    path("new/review/", views.intake_review, name="intake_review"),
    path("overdue/", views.overdue_today, name="overdue"),
    path("logins/", views.officer_logins, name="officer_logins"),
    path("logins/new/", views.officer_login_new, name="officer_login_new"),
    path("logins/<int:pk>/<str:action>/", views.officer_login_action, name="officer_login_action"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/bell/", views.notification_bell, name="notification_bell"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    path("notifications/<int:pk>/open/", views.notification_open, name="notification_open"),
    path("archive/", views.archive, name="archive"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("export/", views.export_page, name="export"),
    path("export/download/", views.export_download, name="export_download"),
]
